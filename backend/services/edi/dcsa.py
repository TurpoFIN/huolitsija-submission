"""DCSA JSON: Track & Trace 2.2 events out, Booking 2.0 request in.

    events = tnt_events(shipment, tracking_events)       # [shipmentEvent | transportEvent | equipmentEvent]
    instr = booking_to_instruction(body)                 # same dict shape as iftmin.parse_iftmin

Event shapes follow the DCSA OpenAPI (DCSA_TNT 2.2.0 + EVENT_DOMAIN 1.0.4, SwaggerHub); tests validate the output
against those schemas (copies in backend/tests/fixtures/edi/). DCSA has no air transport call, so air legs only produce
shipment and equipment events."""
from __future__ import annotations

import uuid

from services.edi import codes

NAMESPACE = uuid.UUID("6f0c1f4e-7a39-4c55-9b1e-2f9a3c6d8e01")


def _event_id(ev: dict) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{ev.get('id')}:{ev.get('code')}"))


def _iso(at: str | None) -> str | None:
    if not at:
        return None
    s = str(at)
    return s if ("+" in s[10:] or s.endswith("Z")) else s + "Z"


def _refs(shipment: dict) -> tuple[list[dict], list[dict]]:
    docs = [{"documentReferenceType": "BKG", "documentReferenceValue": shipment.get("reference")}]
    refs = [{"referenceType": "FF", "referenceValue": shipment.get("reference")}]
    cref = (shipment.get("references") or {}).get("customer_ref")
    if cref:
        refs.append({"referenceType": "CR", "referenceValue": cref})
    for po in (shipment.get("references") or {}).get("po_numbers") or []:
        refs.append({"referenceType": "PO", "referenceValue": po})
    return docs, refs


def _location(loc: dict | None) -> dict | None:
    if not loc:
        return None
    out = {}
    if loc.get("name"):
        out["locationName"] = str(loc["name"])[:100]
    if loc.get("unlocode") and len(str(loc["unlocode"])) == 5:
        out["UNLocationCode"] = loc["unlocode"]
    if loc.get("lat") is not None and loc.get("lon") is not None:
        out["latitude"], out["longitude"] = f"{float(loc['lat']):.4f}", f"{float(loc['lon']):.4f}"
    return out or None


def tnt_events(shipment: dict, tracking_events: list[dict]) -> list[dict]:
    """One DCSA event per tracking event whose code DCSA can express (see codes.DCSA_EVENTS)."""
    docs, refs = _refs(shipment)
    containers = [c for c in (shipment.get("cargo") or {}).get("containers") or []]
    iso_eq = codes.CONTAINER_ISO_OUT.get(containers[0]["type"]) if containers else None
    out = []
    for ev in tracking_events:
        spec = codes.DCSA_EVENTS.get(ev.get("code"))
        if not spec:
            continue
        etype, code, doc_type = spec
        at = _iso(ev.get("at"))
        base = {"eventID": _event_id(ev), "eventCreatedDateTime": _iso(ev.get("created_at") or ev.get("at")),
                "eventType": etype, "eventClassifierCode": "ACT", "eventDateTime": at}
        if etype == "SHIPMENT":
            base.update(eventDateTime=base["eventCreatedDateTime"], shipmentEventTypeCode=code, documentTypeCode=doc_type,
                        documentID=shipment.get("reference"))
            if ev.get("note"):
                base["reason"] = str(ev["note"])[:250]
            base["references"] = refs
        elif etype == "TRANSPORT":
            leg = next((lg for lg in shipment.get("legs") or [] if lg.get("seq") == ev.get("leg_seq")), None) \
                or next((lg for lg in shipment.get("legs") or [] if lg.get("role") == "main"), None) or {}
            mode = codes.DCSA_MODE.get(leg.get("mode") or shipment.get("mode"))
            if not mode:
                continue
            loc = ev.get("location") or {}
            call = {"transportCallID": str(uuid.uuid5(NAMESPACE, f"{shipment.get('id')}:{leg.get('seq')}:{code}")),
                    "modeOfTransport": mode}
            if loc.get("unlocode"):
                call["UNLocationCode"] = loc["unlocode"]
            location = _location(loc)
            if location:
                call["location"] = location
            if leg.get("vessel", {}) and isinstance(leg.get("vessel"), dict) and leg["vessel"].get("imo"):
                call["vessel"] = {"vesselIMONumber": str(leg["vessel"]["imo"]), "vesselName": leg["vessel"].get("name")}
            base.update(transportEventTypeCode=code, transportCall=call, documentReferences=docs, references=refs)
        else:  # EQUIPMENT
            base.update(equipmentEventTypeCode=code, emptyIndicatorCode="LADEN", documentReferences=docs, references=refs)
            if iso_eq:
                base["ISOEquipmentCode"] = iso_eq
            loc = _location(ev.get("location"))
            if loc:
                base["eventLocation"] = loc
        out.append(base)
    return out


# ------------------------------------------------------------------------------------------- Booking 2.0 (subset in)

PARTY_FUNCTIONS = {"OS": "shipper", "CN": "consignee", "N1": "notify", "N2": "notify", "DDR": "forwarder", "DDS": "forwarder"}
LOCATION_TYPES = {"PRE": "origin", "POL": "origin", "PDE": "destination", "POD": "destination"}


def _party(dp: dict) -> dict:
    p = dp.get("party") or {}
    a = p.get("address") or {}
    street = " ".join(x for x in (a.get("street"), a.get("streetNumber")) if x)
    contact = (p.get("partyContactDetails") or [{}])[0] if p.get("partyContactDetails") else {}
    return {"name": p.get("partyName"), "address": {"street": street or None, "city": a.get("city"), "postal_code": a.get("postCode"),
                                                    "country": (a.get("country") or a.get("countryCode") or "").upper() or None},
            "contact": {k: v for k, v in {"name": contact.get("name"), "phone": contact.get("phone"), "email": contact.get("email")}.items() if v}
            or None, "vat_id": p.get("taxReference1")}


def booking_to_instruction(body: dict) -> dict:
    """DCSA Booking 2.0 request → the instruction dict iftmin.parse_iftmin returns."""
    errors: list[str] = []
    ref = body.get("carrierBookingRequestReference") or body.get("bookingRequestReference")
    if not ref:
        errors.append("carrierBookingRequestReference is required (your reference for this booking request).")
    parties = {}
    for dp in body.get("documentParties") or []:
        role = PARTY_FUNCTIONS.get(dp.get("partyFunction"))
        if role and role not in parties:
            parties[role] = _party(dp)
    places: dict = {}
    for sl in body.get("shipmentLocations") or []:
        kind = LOCATION_TYPES.get(sl.get("shipmentLocationTypeCode"))
        loc = sl.get("location") or {}
        if not kind:
            continue
        val = {"unlocode": loc.get("UNLocationCode"), "name": loc.get("locationName"),
               "country": (loc.get("UNLocationCode") or "")[:2] or None}
        if kind not in places or sl.get("shipmentLocationTypeCode") in ("PRE", "PDE"):
            places[kind] = val
    containers: dict = {}
    for eq in body.get("requestedEquipments") or []:
        t = codes.ISO_CONTAINERS.get(str(eq.get("ISOEquipmentCode") or "").upper()) or \
            {"22GP": "20GP", "42GP": "40GP", "45GP": "40HC", "L5GP": "45HC", "22RT": "20RF", "45RT": "40RF"}.get(
                str(eq.get("ISOEquipmentCode") or "").upper())
        if not t:
            errors.append(f"ISOEquipmentCode {eq.get('ISOEquipmentCode')!r} is not one we book.")
            continue
        containers[t] = containers.get(t, 0) + int(eq.get("units") or 1)
    weight = 0.0
    descriptions, hs = [], None
    for c in body.get("commodities") or []:
        w = float(c.get("cargoGrossWeight") or 0)
        unit = (c.get("cargoGrossWeightUnit") or "KGM").upper()
        weight += w * (0.45359237 if unit == "LBR" else 1)
        if c.get("commodityType"):
            descriptions.append(c["commodityType"])
        hs = hs or (c.get("HSCodes") or [None])[0] or c.get("HSCode")
    mode = {"VESSEL": "sea", "RAIL": "rail", "TRUCK": "road", "BARGE": "sea"}.get(
        str(body.get("preCarriageModeOfTransportCode") or body.get("modeOfTransport") or "VESSEL").upper(), "sea")
    cargo = {"load_type": "FCL" if containers else "loose", "containers": [{"type": t, "count": n} for t, n in containers.items()],
             "weight_kg": round(weight, 3) or None, "description": "; ".join(descriptions)[:500] or None, "hs_hint": hs}
    instr = {
        "message_ref": None, "doc_code": "DCSA_BKG", "doc_number": ref, "function": "9", "doc_date": body.get("bookingRequestDateTime"),
        "mode": mode, "origin": places.get("origin"), "destination": places.get("destination"),
        "ready_date": (body.get("expectedDepartureDate") or "")[:10] or None,
        "delivery_date": (body.get("expectedArrivalAtPlaceOfDeliveryStartDate") or "")[:10] or None,
        "incoterm": (body.get("incoTerms") or "").upper() or None, "incoterm_place": None, "carrier_name": None,
        "parties": parties, "references": {"customer_ref": ref, "po_numbers": [r.get("referenceValue") for r in body.get("references") or []
                                                                               if r.get("referenceType") == "PO"], "other": {}},
        "cargo": cargo, "instructions": None, "errors": errors,
    }
    for role in ("shipper", "consignee"):
        p = parties.get(role)
        if not p or not p.get("name"):
            errors.append(f"documentParties needs the {role} ({'OS' if role == 'shipper' else 'CN'}).")
        elif not (p["address"].get("street") and p["address"].get("city") and p["address"].get("country")):
            errors.append(f"The {role}'s address needs street, city and country.")
    if not instr["origin"]:
        errors.append("shipmentLocations needs the place of receipt (PRE) or port of loading (POL).")
    if not instr["destination"]:
        errors.append("shipmentLocations needs the place of delivery (PDE) or port of discharge (POD).")
    if not containers and not cargo["weight_kg"]:
        errors.append("Give requestedEquipments or the commodities' cargoGrossWeight.")
    if instr["incoterm"] and instr["incoterm"] not in codes.INCOTERMS:
        instr["incoterm"] = None
    return instr


DCSA_STATUS = {"29": "CONFIRMED", "23": "PENDING UPDATE", "27": "REJECTED", "1": "CANCELLED"}


def booking_response(instr: dict, result: dict) -> dict:
    out = {"carrierBookingRequestReference": instr.get("doc_number"),
           "bookingStatus": DCSA_STATUS.get(result.get("function"), "RECEIVED"),
           "carrierBookingReference": result.get("shipment_reference"),
           "quoteNumber": result.get("quote_number")}
    if result.get("reasons"):
        out["reason"] = " ".join(result["reasons"])[:5000]
    return {k: v for k, v in out.items() if v is not None}
