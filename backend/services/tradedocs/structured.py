"""Structured (machine-readable) versions of the trade documents.

    build(kind, fmt, ctx, meta) -> dict | str

Formats and the public specifications they follow (copies of the schemas are under backend/data/specs/, the tests
validate every export against them):
- dcsa_ebl_3  DCSA eBL 3.0 `TransportDocument` (DCSA_EBL 3.0.3 OpenAPI, SwaggerHub dcsaorg/DCSA_EBL/3.0.3). This is the
              payload an eBL platform receives through the DCSA eBL Issuance API (`PUT /v3/ebl-issuance-requests`).
- one_record  IATA ONE Record cargo ontology 3.3 (2026-07 standard) JSON-LD: cargo:Waybill (HOUSE) + cargo:Shipment +
              cargo:Piece + cargo:Party; what a ONE Record server (e.g. NE:ONE) stores as logistics objects.
- xfzb        IATA Cargo-XML 3.00 house waybill message (root ns2:HouseWaybill, namespace iata:housewaybill:1 /
              iata:datamodel:3), the message a forwarder sends to the airline / CCS for each house.
- ecmr        UN/CEFACT eCMR (BuyShipPay D23B MultiModalTransport snapshot, JSON schema 2020-12), document type 730
              (road consignment note), the data an e-CMR platform under the e-CMR Additional Protocol (2008) exchanges.
- json        the document's own data (every kind): the snapshot the PDF was printed from, with number and version.
"""
from __future__ import annotations

import re
from xml.sax.saxutils import escape as xesc

from core import config
from services.tradedocs import data as D

UNLOC = re.compile(r"^[A-Z]{2}[A-Z2-9]{3}$")


def _iso(dt: str | None) -> str | None:
    return dt[:10] if dt else None


def _clip(s, n):
    s = (s or "").strip()
    return s[:n].strip() if s else s


def _loc(p: dict | None) -> dict:
    p = p or {}
    out = {}
    if p.get("unlocode") and UNLOC.match(p["unlocode"]):
        out["UNLocationCode"] = p["unlocode"]
    if p.get("name"):
        out["locationName"] = _clip(p["name"], 100)
    return out


def _dcsa_address(p: dict) -> dict | None:
    a = p.get("address") or {}
    if not (a.get("street") and a.get("city") and a.get("country")):
        return None
    out = {"street": _clip(a["street"], 70), "city": _clip(a["city"], 35), "countryCode": a["country"][:2]}
    if a.get("postal_code"):
        out["postCode"] = _clip(a["postal_code"], 10)
    return out


def _dcsa_party(p: dict | None, *, codes_required=False) -> dict | None:
    if not p:
        return None
    out = {"partyName": _clip(p["name"], 70)}
    addr = _dcsa_address(p)
    if addr:
        out["address"] = addr
    codes = []
    for scheme, val in (("EORI", p.get("eori")), ("VAT", p.get("vat_id")), ("BUSINESS_ID", p.get("business_id"))):
        if val:
            codes.append({"codeListProvider": "ZZZ", "codeListName": scheme, "partyCode": _clip(val, 150)})
    if codes:
        out["identifyingCodes"] = codes
    elif codes_required:
        out["identifyingCodes"] = [{"codeListProvider": "ZZZ", "codeListName": "NAME", "partyCode": _clip(p["name"], 150)}]
    tax = [{"type": "EORI", "countryCode": p["eori"][:2], "value": _clip(p["eori"], 35)}] if p.get("eori") and re.match(r"^[A-Z]{2}", p["eori"]) else []
    if tax:
        out["taxLegalReferences"] = tax
    if p.get("contact_name") and (p.get("email") or p.get("phone")):
        c = {"name": _clip(p["contact_name"], 35)}
        if p.get("email"):
            c["email"] = p["email"]
        if p.get("phone"):
            c["phone"] = p["phone"]
        out["partyContactDetails"] = [c]
    return out


def _lines(text: str, width: int) -> list[str]:
    """DCSA descriptionOfGoods: an array of lines of at most 35 characters (as printed in the B/L goods column)."""
    import textwrap
    return [ln.strip() for ln in textwrap.wrap(text, width) if ln.strip()][:150] or ["-"]


def dcsa_missing(ctx: dict) -> list[str]:
    """What stops this house B/L from being issued as an eBL (the PDF is unaffected)."""
    miss = []
    if not D.issuer().get("scac"):
        miss.append("TRADEDOCS_ISSUER_SCAC (NMFTA SCAC of the forwarder as issuing carrier)")
    if not ctx["containers"]:
        miss.append("container number (equipmentReference)")
    if not ctx["main_leg"].get("vessel"):
        miss.append("vessel name")
    if not any(g.get("hs_code") and re.fullmatch(r"\d{6,10}", str(g["hs_code"])) for g in ctx["goods"]):
        miss.append("HS code (6-10 digits)")
    return miss


def dcsa_ebl(ctx: dict, meta: dict) -> dict:
    iss = D.issuer()
    neg = bool(ctx.get("negotiable"))
    m = ctx["main_leg"]
    status = {"draft": "DRAFT", "issued": "ISSUED", "superseded": "ISSUED", "cancelled": "VOIDED"}.get(meta.get("status"), "DRAFT")
    containers = ctx["containers"] or []
    eq_refs = [c["number"] for c in containers] or ["UNKNOWN"]
    booking = _clip(ctx["refs"].get("booking") or ctx["shipment"]["reference"], 35)
    tot = ctx["totals"]
    consignment = []
    for i, g in enumerate(ctx["goods"]):
        hs = [str(g["hs_code"])[:10]] if g.get("hs_code") and re.fullmatch(r"\d{6,10}", str(g["hs_code"])[:10]) else ["000000"]
        pk = int(g.get("packages") or (tot.get("packages") if i == 0 else 0) or 1)
        outer = {"packageCode": D.package_code(g.get("package_type")), "numberOfPackages": pk,
                 "description": _clip(g.get("package_type") or "packages", 100)}
        dgs = []
        if i == 0:
            for d in ctx["dangerous_goods"]:
                x = {"UNNumber": d["un_number"].replace("UN", ""), "properShippingName": _clip(d.get("proper_shipping_name") or "", 250),
                     "imoClass": str(d["class"])[:4], "isMarinePollutant": bool(d.get("marine_pollutant")),
                     "isLimitedQuantity": bool(d.get("limited_quantity"))}
                pg = {"I": 1, "II": 2, "III": 3}.get(str(d.get("packing_group") or "").strip())
                if pg:
                    x["packingGroup"] = pg
                if d.get("ems"):
                    x["EMSNumber"] = d["ems"][:7]
                dgs.append(x)
        if dgs:
            outer["dangerousGoods"] = dgs
        gross = float(g.get("gross_kg") or (tot.get("gross_kg") if i == 0 else 0) or 0)
        items = []
        share = len(eq_refs)
        for ref in eq_refs:
            items.append({"equipmentReference": ref[:11], "cargoGrossWeight": {"value": round(gross / share, 3), "unit": "KGM"},
                          "outerPackaging": {**outer, "numberOfPackages": max(1, round(pk / share))}})
        ci = {"carrierBookingReference": booking, "descriptionOfGoods": _lines(g.get("description") or "", 35),
              "HSCodes": hs, "cargoItems": items}
        if g.get("marks"):
            ci["shippingMarks"] = [g["marks"]]
        consignment.append(ci)
    equipment = []
    for c in containers or [{"number": "UNKNOWN", "type": None, "seal": None}]:
        e = {"equipment": {"equipmentReference": c["number"][:11]}, "isShipperOwned": False,
             "seals": [{"number": (c.get("seal") or "NOT SEALED")[:15]}]}
        iso = {"20GP": "22G1", "20": "22G1", "40GP": "42G1", "40": "42G1", "40HC": "45G1", "45HC": "L5G1", "20RF": "22R1",
               "40RF": "42R1", "40HR": "45R1"}.get(str(c.get("type") or "").upper())
        if iso:
            e["equipment"]["ISOEquipmentCode"] = iso
        equipment.append(e)
    pol, pod = ctx["port_of_loading"], ctx["port_of_discharge"]
    transports = {"plannedDepartureDate": _iso(m.get("etd") or ctx["shipment"].get("etd")),
                  "plannedArrivalDate": _iso(m.get("eta") or ctx["shipment"].get("eta")),
                  "portOfLoading": _loc(pol), "portOfDischarge": _loc(pod),
                  "vesselVoyages": [{"vesselName": _clip(m.get("vessel") or "TBN", 50),
                                     "carrierExportVoyageNumber": _clip(m.get("voyage") or "TBN", 50)}]}
    if ctx["place_of_receipt"] and ctx["place_of_receipt"] != pol:
        transports["placeOfReceipt"] = _loc(ctx["place_of_receipt"])
    if ctx["place_of_delivery"] and ctx["place_of_delivery"] != pod:
        transports["placeOfDelivery"] = _loc(ctx["place_of_delivery"])
    pre = next((lg for lg in ctx["legs"] if lg["mode"] != "sea" and (lg["seq"] or 0) < (m.get("seq") or 0)), None)
    on = next((lg for lg in ctx["legs"] if lg["mode"] != "sea" and (lg["seq"] or 0) > (m.get("seq") or 0)), None)
    mode_name = {"road": "Truck", "rail": "Rail", "air": "Air"}
    if pre:
        transports["preCarriageBy"] = mode_name.get(pre["mode"], "Truck")
    if on:
        transports["onCarriageBy"] = mode_name.get(on["mode"], "Truck")
    payable = ctx["port_of_loading"] if ctx["freight"]["terms"] == "prepaid" else ctx["port_of_discharge"]
    issuing = {"partyName": _clip(iss["name"], 70), "address": _dcsa_address(iss) or
               {"street": "Not yet registered", "city": iss["address"].get("city") or "Helsinki", "countryCode": "FI"}}
    if iss.get("business_id"):
        issuing["identifyingCodes"] = [{"codeListProvider": "ZZZ", "codeListName": "FI_BUSINESS_ID", "partyCode": iss["business_id"]}]
    parties = {"shipper": _dcsa_party(ctx["shipper"]) or {"partyName": "UNKNOWN"}, "issuingParty": issuing}
    if not neg and ctx["consignee"]:
        parties["consignee"] = _dcsa_party(ctx["consignee"], codes_required=True)
    if ctx["notify"]:
        parties["notifyParties"] = [_dcsa_party(ctx["notify"])]
    elif neg and ctx["consignee"]:
        parties["notifyParties"] = [_dcsa_party(ctx["consignee"])]
    doc = {
        "transportDocumentReference": meta["doc_no"][:20],
        "transportDocumentStatus": status,
        "transportDocumentTypeCode": "BOL" if neg else "SWB",
        "isShippedOnBoardType": bool(ctx["dates"].get("shipped_on_board")),
        "freightPaymentTermCode": "PRE" if ctx["freight"]["terms"] == "prepaid" else "COL",
        "isElectronic": True,
        "isToOrder": neg,
        "numberOfOriginalsWithCharges": 0, "numberOfOriginalsWithoutCharges": 1 if neg else 0,
        "termsAndConditions": "Carriage under the Hague-Visby Rules (Finnish Maritime Code ch. 13) and the General Conditions of "
                              "the Nordic Association of Freight Forwarders NSAB 2015. The forwarder acts as contracting carrier.",
        "receiptTypeAtOrigin": "CY", "deliveryTypeAtDestination": "SD" if on else "CY",
        "cargoMovementTypeAtOrigin": "FCL" if containers else "LCL", "cargoMovementTypeAtDestination": "FCL" if containers else "LCL",
        "issueDate": _iso(ctx["dates"].get("issue")),
        "carrierCode": (iss.get("scac") or "HUOL")[:4], "carrierCodeListProvider": "NMFTA",
        "invoicePayableAt": {"UNLocationCode": payable["unlocode"]} if payable.get("unlocode") and UNLOC.match(payable["unlocode"])
        else {"freeText": _clip(payable.get("name") or "Helsinki", 35)},
        "placeOfIssue": {"UNLocationCode": "FIHEL"},
        "partyContactDetails": [{"name": _clip(iss["name"], 35), "email": iss.get("email") or config.COMPANY_EMAIL}],
        "documentParties": parties,
        "consignmentItems": consignment,
        "utilizedTransportEquipments": equipment,
        "transports": transports,
        "references": [{"type": "FF", "value": ctx["shipment"]["reference"]}] + (
            [{"type": "CR", "value": _clip(ctx["refs"]["customer"], 35)}] if ctx["refs"].get("customer") else []),
    }
    if doc["isShippedOnBoardType"]:
        doc["shippedOnBoardDate"] = _iso(ctx["dates"]["shipped_on_board"])
        doc["displayedShippedOnBoardReceivedForShipment"] = f"Shipped on board {_iso(ctx['dates']['shipped_on_board'])}"
    else:
        doc["receivedForShipmentDate"] = _iso(ctx["dates"].get("issue"))
    if tot.get("volume_m3"):
        for ci in doc["consignmentItems"][:1]:
            for item in ci["cargoItems"]:
                item["cargoGrossVolume"] = {"value": round(float(tot["volume_m3"]) / len(ci["cargoItems"]), 3), "unit": "MTQ"}
    return doc


# ------------------------------------------------------------------------------------------------ air
def _iata(p: dict | None) -> str | None:
    """IATA airport code from the location (iata field, or the UN/LOCODE's last three letters when it is the airport's)."""
    if not p:
        return None
    if p.get("iata"):
        return p["iata"]
    code = p.get("unlocode") or ""
    if len(code) == 3 and code.isalpha():
        return code
    return None


def one_record(ctx: dict, meta: dict) -> dict:
    """ONE Record 3.3 JSON-LD for the house waybill (embedded objects; a server assigns @id on POST)."""
    tot = ctx["totals"]
    base = (config.APP_BASE_URL or "https://huolitsija.fi").rstrip("/") + "/onerecord/logistics-objects/"

    def val(v, unit):
        return {"@type": "cargo:Value", "cargo:numericalValue": {"@type": "xsd:double", "@value": str(float(v or 0))},
                "cargo:unit": {"@id": f"https://onerecord.iata.org/ns/coreCodeLists#MeasurementUnitCode_{unit}"}}

    def loc(p):
        code = _iata(p)
        out = {"@type": "cargo:Location", "cargo:locationName": (p or {}).get("name") or ""}
        if code:
            out["cargo:locationCodes"] = [{"@type": "cargo:CodeListElement", "cargo:code": code,
                                           "cargo:codeListName": "IATA three-letter location identifier"}]
        elif (p or {}).get("unlocode"):
            out["cargo:locationCodes"] = [{"@type": "cargo:CodeListElement", "cargo:code": p["unlocode"], "cargo:codeListName": "UN/LOCODE"}]
        return out

    def party(p, role):
        if not p:
            return None
        a = p.get("address") or {}
        org = {"@type": "cargo:Organization", "cargo:name": p["name"],
               "cargo:basedAtLocation": {"@type": "cargo:Location", "cargo:address": {
                   "@type": "cargo:Address", "cargo:streetAddressLines": [a.get("street") or ""], "cargo:postalCode": a.get("postal_code") or "",
                   "cargo:cityName": a.get("city") or "",
                   "cargo:country": {"@id": f"https://onerecord.iata.org/ns/coreCodeLists#CountryCode_{a.get('country') or 'FI'}"}}}}
        return {"@type": "cargo:Party", "cargo:partyRole": {"@id": f"https://onerecord.iata.org/ns/coreCodeLists#ParticipantIdentifier_{role}"},
                "cargo:partyDetails": org}

    pieces = []
    for g in ctx["goods"]:
        pieces.append({"@type": "cargo:Piece", "cargo:goodsDescription": g.get("description") or "",
                       "cargo:grossWeight": val(g.get("gross_kg"), "KGM"),
                       "cargo:slac": int(g.get("packages") or 1)})
    hawb_no = meta["doc_no"]
    master = ctx["refs"].get("awb_master") or ""
    m = re.match(r"^(\d{3})-?(\d{8})$", master)
    waybill = {
        "@context": {"cargo": "https://onerecord.iata.org/ns/cargo#", "api": "https://onerecord.iata.org/ns/api#",
                     "xsd": "http://www.w3.org/2001/XMLSchema#"},
        "@id": base + "waybill-" + hawb_no, "@type": "cargo:Waybill",
        "cargo:waybillType": {"@id": "https://onerecord.iata.org/ns/cargo#HOUSE"},
        "cargo:waybillNumber": hawb_no,
        "cargo:departureLocation": loc(ctx["port_of_loading"]), "cargo:arrivalLocation": loc(ctx["port_of_discharge"]),
        "cargo:involvedParties": [x for x in (party(ctx["shipper"], "SHP"), party(ctx["consignee"], "CNE"),
                                              party(ctx["notify"], "NFY"), party(D.issuer(), "FFW")) if x],
        "cargo:shipment": {
            "@id": base + "shipment-" + hawb_no, "@type": "cargo:Shipment",
            "cargo:goodsDescription": "; ".join(g.get("description") or "" for g in ctx["goods"])[:500],
            "cargo:totalGrossWeight": val(tot.get("gross_kg"), "KGM"),
            "cargo:totalDimensions": {"@type": "cargo:Dimensions", "cargo:volume": val(tot.get("volume_m3"), "MTQ")} if tot.get("volume_m3") else None,
            "cargo:pieces": pieces},
        "cargo:waybillLineItems": None,
    }
    if m:
        waybill["cargo:masterWaybill"] = {"@type": "cargo:Waybill", "cargo:waybillType": {"@id": "https://onerecord.iata.org/ns/cargo#MASTER"},
                                          "cargo:waybillPrefix": m.group(1), "cargo:waybillNumber": m.group(2)}
    return _prune(waybill)


def _prune(x):
    if isinstance(x, dict):
        return {k: _prune(v) for k, v in x.items() if v is not None}
    if isinstance(x, list):
        return [_prune(v) for v in x if v is not None]
    return x


def xfzb(ctx: dict, meta: dict) -> str:
    """Cargo-XML 3.00 HouseWaybill (XFZB) following IATA's message structure (sample: Riege one-record-converter)."""
    tot = ctx["totals"]
    iss = D.issuer()
    origin, dest = _iata(ctx["port_of_loading"]) or "", _iata(ctx["port_of_discharge"]) or ""
    master = ctx["refs"].get("awb_master") or ""
    pp = ctx["freight"]["terms"] == "prepaid"
    cur = ctx["freight"]["currency"] or "EUR"
    issued = (ctx["dates"].get("issue") or "")[:19]

    def party(tag, p):
        if not p:
            return ""
        a = p.get("address") or {}
        return (f"<{tag}><Name>{xesc(p['name'][:70])}</Name><PostalStructuredAddress>"
                f"<PostcodeCode>{xesc(a.get('postal_code') or '')}</PostcodeCode><StreetName>{xesc(a.get('street') or '')}</StreetName>"
                f"<CityName>{xesc(a.get('city') or '')}</CityName><CountryID>{xesc(a.get('country') or '')}</CountryID>"
                f"</PostalStructuredAddress></{tag}>")

    items = "".join(
        f"<IncludedHouseConsignmentItem><SequenceNumeric>{g['seq']}</SequenceNumeric>"
        + (f"<TypeCode>{xesc(str(g['hs_code']))}</TypeCode>" if g.get("hs_code") else "")
        + f"<GrossWeightMeasure unitCode=\"KGM\">{float(g.get('gross_kg') or 0):.1f}</GrossWeightMeasure>"
        f"<PieceQuantity>{int(g.get('packages') or 1)}</PieceQuantity>"
        f"<NatureIdentificationTransportCargo><Identification>{xesc((g.get('description') or '')[:70])}</Identification></NatureIdentificationTransportCargo>"
        f"</IncludedHouseConsignmentItem>" for g in ctx["goods"])
    notes = ""
    for role, p in (("SHP", ctx["shipper"]), ("CNE", ctx["consignee"])):
        if p and p.get("eori"):
            notes += (f"<IncludedCustomsNote><ContentCode>T</ContentCode><Content>{xesc(p['eori'])}</Content>"
                      f"<SubjectCode>{role}</SubjectCode><CountryID>{xesc(p['eori'][:2])}</CountryID></IncludedCustomsNote>")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<ns2:HouseWaybill xmlns="iata:datamodel:3" xmlns:ns2="iata:housewaybill:1">'
        f"<ns2:MessageHeaderDocument><ID>{xesc(meta['doc_no'])}_{xesc(master)}_v{meta['version']}</ID><Name>House Waybill</Name>"
        f"<TypeCode>703</TypeCode><IssueDateTime>{issued}</IssueDateTime>"
        f"<PurposeCode>{'Update' if meta.get('amendment') else 'Creation'}</PurposeCode><VersionID>3.00</VersionID>"
        f"<SenderParty><PrimaryID schemeID=\"C\">HUOLITSIJA</PrimaryID></SenderParty></ns2:MessageHeaderDocument>"
        f"<ns2:BusinessHeaderDocument><ID>{xesc(meta['doc_no'])}</ID>"
        f"<SignatoryConsignorAuthentication><Signatory>{xesc(((ctx['shipper'] or {}).get('name') or '')[:20].upper())}</Signatory></SignatoryConsignorAuthentication>"
        f"<SignatoryCarrierAuthentication><ActualDateTime>{issued}</ActualDateTime><Signatory>{xesc(iss['name'][:20].upper())}</Signatory>"
        f"<IssueAuthenticationLocation><Name>{xesc(iss['address'].get('city') or 'HELSINKI')}</Name></IssueAuthenticationLocation>"
        f"</SignatoryCarrierAuthentication></ns2:BusinessHeaderDocument>"
        f"<ns2:MasterConsignment><TransportContractDocument><ID>{xesc(master)}</ID></TransportContractDocument>"
        f"<OriginLocation><ID>{origin}</ID></OriginLocation><FinalDestinationLocation><ID>{dest}</ID></FinalDestinationLocation>"
        f"<IncludedHouseConsignment><ID>{xesc(meta['doc_no'])}</ID>"
        f"<NilCarriageValueIndicator>true</NilCarriageValueIndicator>"
        f"<NilCustomsValueIndicator>{'false' if tot.get('amount') else 'true'}</NilCustomsValueIndicator>"
        + (f"<DeclaredValueForCustomsAmount currencyID=\"{xesc(tot.get('currency') or cur)}\">{float(tot['amount']):.2f}</DeclaredValueForCustomsAmount>" if tot.get("amount") else "")
        + "<NilInsuranceValueIndicator>true</NilInsuranceValueIndicator>"
        f"<TotalChargePrepaidIndicator>{'true' if pp else 'false'}</TotalChargePrepaidIndicator>"
        f"<TotalDisbursementPrepaidIndicator>{'true' if pp else 'false'}</TotalDisbursementPrepaidIndicator>"
        f"<IncludedTareGrossWeightMeasure unitCode=\"KGM\">{float(tot.get('gross_kg') or 0):.1f}</IncludedTareGrossWeightMeasure>"
        + (f"<GrossVolumeMeasure unitCode=\"MTQ\">{float(tot['volume_m3']):.3f}</GrossVolumeMeasure>" if tot.get("volume_m3") else "")
        + f"<TotalPieceQuantity>{int(tot.get('packages') or 1)}</TotalPieceQuantity>"
        f"<SummaryDescription>{xesc(('; '.join(g.get('description') or '' for g in ctx['goods']))[:70])}</SummaryDescription>"
        + party("ConsignorParty", ctx["shipper"]) + party("ConsigneeParty", ctx["consignee"])
        + f"<OriginLocation><ID>{origin}</ID></OriginLocation><FinalDestinationLocation><ID>{dest}</ID></FinalDestinationLocation>"
        + notes + items
        + "</IncludedHouseConsignment></ns2:MasterConsignment></ns2:HouseWaybill>")


# ------------------------------------------------------------------------------------------------ road
def ecmr(ctx: dict, meta: dict) -> dict:
    """UN/CEFACT eCMR (D23B) consignment for the international road carriage (UN/CEFACT JSON: identifiers, codes,
    texts and measures are objects with `content`)."""
    leg = dict((ctx["road_legs_international"] or [lg for lg in ctx["legs"] if lg["mode"] == "road"] or [{}])[0])
    if ctx["shipment"]["mode"] == "road" and ctx["legs"]:
        leg["from"], leg["to"] = ctx["legs"][0]["from"], ctx["legs"][-1]["to"]  # CMR art. 2: ro-ro legs included
    tot = ctx["totals"]

    def T(x):
        return {"content": str(x)}

    def I(x):
        return {"content": str(x)}

    def Mz(v, unit):
        return {"content": f"{float(v or 0):.3f}".rstrip("0").rstrip("."), "unitCode": unit}

    def address(a):
        a = a or {}
        out = {}
        if a.get("street"):
            out["streetName"] = [T(a["street"])]
        if a.get("postal_code"):
            out["postcodeCode"] = [{"content": a["postal_code"]}]
        if a.get("city"):
            out["cityName"] = [T(a["city"])]
        if a.get("country"):
            out["countryId"] = I(a["country"])
        return out

    def party(p):
        if not p:
            return None
        out = {"name": [T(p["name"])]}
        addr = address(p.get("address"))
        if addr:
            out["postalTradeAddress"] = addr
        if p.get("eori"):
            out["id"] = [I(p["eori"])]
        return out

    def place(p, type_code):
        p = p or {}
        out = {"name": [T(p.get("name") or "")], "typeCode": [{"content": type_code}]}
        if p.get("unlocode"):
            out["id"] = [I(p["unlocode"])]
        addr = address(p.get("address") or {"city": p.get("name")})
        addr.pop("countryId", None)  # the eCMR snapshot allows no country here; the UN/LOCODE id carries it
        if addr:
            out["postalTradeAddress"] = addr
        return out

    items = []
    for g in ctx["goods"]:
        it = {"sequenceNumeric": {"content": str(g["seq"])}, "grossWeight": [Mz(g.get("gross_kg"), "KGM")],
              "natureIdCargo": [{"id": [T(g.get("description") or "")]}],
              "transportLogisticsPackage": [{"itemQuantity": {"content": str(int(g.get("packages") or 1))},
                                             "typeCode": [{"content": D.package_code(g.get("package_type"))}]}]}
        if g.get("volume_m3"):
            it["grossVolume"] = [Mz(g["volume_m3"], "MTQ")]
        if g.get("origin_country"):
            it["originTradeCountry"] = {"id": [I(g["origin_country"])]}
        items.append(it)
    remarks = [T(f"Version {meta['version']}")]
    if meta.get("amendment"):
        remarks.append(T(f"Amendment {meta['amendment']['number']}: {meta['amendment']['reason']}"))
    for d in ctx["dangerous_goods"]:
        remarks.append(T(f"{d['un_number']} {d.get('proper_shipping_name') or ''}, {d['class']}"
                         + (f", {d['packing_group']}" if d.get("packing_group") else "") + ", (E)"))
    consignment = {
        "id": [I(meta["doc_no"])], "freightForwarderAssignedId": I(ctx["shipment"]["reference"]),
        "grossWeight": [Mz(tot.get("gross_kg"), "KGM")],
        "consignmentItemQuantity": {"content": str(len(items))},
        "consignor": party(ctx["shipper"]), "consignee": party(ctx["consignee"]),
        "carrier": {"name": [T(leg.get("carrier") or "To be nominated")]},
        "carrierAcceptanceLogisticsLocation": place(leg.get("from"), "5"),
        "consigneeReceiptLogisticsLocation": place(leg.get("to"), "11"),
        "includedConsignmentItem": items,
    }
    if tot.get("volume_m3"):
        consignment["grossVolume"] = [Mz(tot["volume_m3"], "MTQ")]
    if ctx["dates"].get("taking_over"):
        consignment["pickUpEvent"] = {"actualOccurrenceDateTime": ctx["dates"]["taking_over"]}
    doc = {
        "exchangedDocument": {"id": I(meta["doc_no"]), "typeCode": {"content": "730"}, "issueDateTime": ctx["dates"].get("issue"),
                              "remarks": remarks,
                              "issueLogisticsLocation": {"name": [T(ctx["issuer"]["address"].get("city") or "Helsinki")]},
                              "issuer": {"name": [T(ctx["issuer"]["name"])]}},
        "specifiedConsignment": [_prune(consignment)],
    }
    return _prune(doc)


def plain(kind: str, ctx: dict, meta: dict) -> dict:
    return {"document": {"kind": kind, "number": meta["doc_no"], "version": meta["version"], "status": meta.get("status"),
                         "issued_at": meta.get("issued_at"), "amendment": meta.get("amendment")}, "data": ctx}


def build(kind: str, fmt: str, ctx: dict, meta: dict):
    if fmt == "json":
        return plain(kind, ctx, meta)
    if fmt == "dcsa_ebl_3":
        return dcsa_ebl(ctx, meta)
    if fmt == "one_record":
        return one_record(ctx, meta)
    if fmt == "xfzb":
        return xfzb(ctx, meta)
    if fmt == "ecmr":
        return ecmr(ctx, meta)
    raise ValueError(f"unknown format {fmt}")
