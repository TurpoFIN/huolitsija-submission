"""Per-system TEST message check for a test-tenant shipment (s4 customs go-live).

For one TEST shipment of a test tenant, and for each authority system that applies to it (import FI415B, export
FI515A, transit FI015A, ICS2 ENS), this builds the business message with the app's own builders, validates it
against the official schema (Tulli v1.21 package / Tulli NCTS MIG 2.2.0 / EU ICS2 3.2.0.0), wraps it in the
transport envelope that would carry it, and runs the read-only EU EOS EORI check on the identifiers in it.

Nothing is stored and nothing is sent: no declaration, message or company identifier goes to Tulli or the EU access
point. Every value the shipment and its stored declaration do not hold is filled with a value marked TEST, and each
one is listed in `test_values`, so the result never passes off a placeholder as shipment data.
"""
from __future__ import annotations

import asyncio
import hashlib
from copy import deepcopy

from lxml import etree

from core.db import db
from core.util import NO_ID, now_iso
from services import customs_readiness as ready
from services import ics2_gateway
from services.customs_messages import ics2, ncts_fi, tulli_export, tulli_import
from services.customs_messages.schema_codec import validate as schema_validate

EU = set("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split())
TULLI_APPLICATION = {"import": "IMP", "export": "EXP", "transit": "TRA"}
# Customs offices for the TEST messages: Tulli's own office codes from the EU customs office list (COL).
OFFICE_BY_UNLOCODE = {"FIHEL": "FI009801"}  # Meritulli Vuosaari satama V6 (COL: DEP DES ENT EXP EXT TRA)
TRANSPORT_DOC = {"sea": ("bill_of_lading", "N705"), "air": ("air_waybill", "N740"), "road": ("cmr", "N730"),
                 "rail": ("cim", "N720")}
MODE_CODE = {"sea": "1", "rail": "2", "road": "3", "air": "4"}
ENS_TYPE = {"sea": "IE3F15", "air": "IE3F26", "road": "IE3F50", "rail": "IE3F51"}


class TestValues(list):
    def take(self, path: str, value, why: str):
        self.append({"path": path, "value": value, "why": why})
        return value


def systems_for(shipment: dict) -> list[str]:
    o = ((shipment.get("origin") or {}).get("country") or "").upper()
    d = ((shipment.get("destination") or {}).get("country") or "").upper()
    if d in EU and o not in EU:
        return ["import", "ics2"]
    if o in EU and d not in EU:
        return ["export", "transit"]
    return []


def _party(p: dict | None) -> dict | None:
    if not p:
        return None
    a = p.get("address") or {}
    if isinstance(a, str):
        a = {"street": a}
    return {"name": p.get("name"), "eori": p.get("eori"), "address": {
        "street": a.get("street"), "city": a.get("city"), "postal_code": a.get("postal_code"),
        "country": a.get("country") or p.get("country")}}


def _company_party(company: dict) -> dict:
    return {"name": company.get("name"), "eori": company.get("eori"), "business_id": company.get("business_id"),
            "vat_id": company.get("vat_id"), "address": dict(company.get("address") or {})}


def _lrn(system: str, shipment: dict) -> str:
    # 14 characters: the shortest LRN/Identification limit across FI415B, FI515A and FI015A
    return f"TEST{system[0].upper()}{''.join(ch for ch in shipment['reference'] if ch.isdigit())[-9:]}"


def _cargo_item(shipment: dict, tv: TestValues, system: str) -> dict:
    c = shipment.get("cargo") or {}
    hs = str(c.get("hs_hint") or "")
    if len(hs) < 6:
        hs = tv.take("items[0].hs_code", "000000", "shipment has no HS code")
    cn = hs[:6] + tv.take("items[0].cn_code[6:8]", "00", "shipment holds the HS subheading only; CN digits for TEST") \
        if len(hs) < 8 else hs[:8]
    return {"seq": 1, "description": c.get("description"), "hs_code": hs[:6], "cn_code": cn,
            "gross_kg": c.get("weight_kg"), "net_kg": c.get("net_weight_kg") or c.get("weight_kg"),
            "packages": c.get("packages"), "package_type": c.get("package_type"),
            "origin_country": c.get("origin_country") or (shipment.get("origin") or {}).get("country"),
            "statistical_value_cents": c.get("value_cents"),
            "invoice_amount": (c.get("value_cents") or 0) / 100 if c.get("value_cents") else None,
            "currency": c.get("currency")}


# ------------------------------------------------------------------------------------------------- declarations
def import_declaration(shipment: dict, stored: dict | None, company: dict, tv: TestValues) -> dict:
    d = deepcopy(stored) if stored else {
        "declaration_type": "IM", "additional_declaration_type": "A", "importer": _company_party(company),
        "declarant": _company_party(company), "exporter": _party((shipment.get("parties") or {}).get("shipper")),
        "items": [_cargo_item(shipment, tv, "import")], "transport": {}}
    d["lrn"] = d["message_id"] = _lrn("import", shipment)
    shipper = _party((shipment.get("parties") or {}).get("shipper"))
    for role in ("exporter", "seller"):
        line = ((d.get(role) or {}).get("address") or {})
        line = line if isinstance(line, str) else line.get("street") or ""
        if shipper and len(line) > 70:  # MIG: Address/Line max 70; the shipment holds the structured address
            d[role] = {**d[role], "address": shipper["address"]}
            tv.take(f"{role}.address", shipper["address"]["street"],
                    "stored street holds the whole address (over 70 characters); shipment party address used")
    tr = d.setdefault("transport", {})
    mode = shipment.get("mode") or tr.get("mode") or "sea"
    tr.setdefault("mode_at_border", MODE_CODE.get(mode))
    tr.setdefault("dispatch_country", (shipment.get("origin") or {}).get("country"))
    tr.setdefault("destination_country", (shipment.get("destination") or {}).get("country"))
    td = tr.get("transport_document") or {}
    if not td.get("number"):
        kind, code = TRANSPORT_DOC.get(mode, TRANSPORT_DOC["sea"])
        tr["transport_document"] = {"type": kind, "code": code,
                                    "number": tv.take("transport.transport_document.number",
                                                      f"TEST-{code}-{shipment['reference']}",
                                                      "no transport document on the shipment yet")}
    unloc = (shipment.get("destination") or {}).get("unlocode")
    if not (d.get("office") or {}).get("code"):
        d["office"] = {"code": tv.take("office.code", OFFICE_BY_UNLOCODE.get(unloc, "FITEST01"),
                                       f"presentation office for {unloc} not chosen on the declaration")}
    if not d.get("goods_location"):
        d["goods_location"] = {"type": "A", "qualifier": "CustomsOffice",
                               "identification": tv.take("goods_location.identification", d["office"]["code"],
                                                         "goods location not recorded; the customs office is used")}
    if not (d.get("representative") or {}).get("eori"):
        d.pop("representative", None)  # the forwarder's own EORI is pending registration: TEST files as the importer
        tv.take("representative", None, "Huolitsija's own EORI is pending (Y-tunnus); TEST message has no agent")
    for it in d.get("items") or []:
        if not it.get("packages"):
            it["packages"] = tv.take(f"items[{it.get('seq')}].packages", 1, "package count split not recorded")
    return d


def export_declaration(shipment: dict, company: dict, tv: TestValues) -> dict:
    mode = shipment.get("mode") or "road"
    kind, code = TRANSPORT_DOC.get(mode, TRANSPORT_DOC["road"])
    origin, dest = shipment.get("origin") or {}, shipment.get("destination") or {}
    consignee = _party((shipment.get("parties") or {}).get("consignee"))
    office = OFFICE_BY_UNLOCODE.get(origin.get("unlocode"), "FITEST01")
    return {
        "lrn": _lrn("export", shipment), "message_id": _lrn("export", shipment),
        "declaration_type": "EX", "additional_declaration_type": "A",
        "declarant": _company_party(company), "exporter": _company_party(company), "consignee": consignee,
        "security": "0",
        "office": {"code": tv.take("office.code", office, f"export office for {origin.get('unlocode')}")},
        "exit_office": {"code": tv.take("exit_office.code", office, "exit office not chosen on the shipment")},
        "transport": {"mode_at_border": MODE_CODE.get(mode), "inland_mode": MODE_CODE.get(mode),
                      "dispatch_country": origin.get("country"), "destination_country": dest.get("country"),
                      "transport_document": {"type": kind, "code": code, "number": tv.take(
                          "transport.transport_document.number", f"TEST-{code}-{shipment['reference']}",
                          "no transport document on the shipment yet")}},
        "goods_location": {"type": "A", "qualifier": "CustomsOffice", "identification": office},
        "submitter": {"name": company.get("name"),
                      "email": (company.get("signatory") or {}).get("email") or company.get("billing_email")},
        "delivery_terms": {"incoterm": shipment.get("incoterm"), "place": shipment.get("incoterm_place")},
        "invoice": {"total": ((shipment.get("cargo") or {}).get("value_cents") or 0) / 100 or None,
                    "currency": (shipment.get("cargo") or {}).get("currency")},
        "items": [_cargo_item(shipment, tv, "export")]}


def transit_declaration(shipment: dict, company: dict, tv: TestValues) -> dict:
    origin, dest = shipment.get("origin") or {}, shipment.get("destination") or {}
    c = shipment.get("cargo") or {}
    lrn = _lrn("transit", shipment)
    dep = OFFICE_BY_UNLOCODE.get(origin.get("unlocode"), "FITEST01")
    destination_office = tv.take("office_of_destination", f"{dest.get('country')}TEST01",
                                 "office of destination not chosen on the shipment")
    gross = str(c.get("weight_kg") or 0)
    national = {
        "Identification": lrn, "FunctionalReference": {"@function": "LRN", "#text": lrn},
        "Type": [{"@function": "DeclarationType", "#text": "T1"},
                 {"@function": "AdditionalDeclarationType", "#text": "A"}],
        "BindingItinerary": "0", "Language": "FI", "ReducedDatasetIndicator": "0", "Security": "0",
        "Principal": {"Identification": company.get("eori"), "Role": "Y"},
        "Submitter": {"Name": company.get("name"), "Communication": {
            "Identification": (company.get("signatory") or {}).get("email") or company.get("billing_email"),
            "Type": "EM"}},
        "Consignment": {"TotalGrossMass": gross, "LoadingLocation": {"Address": {"Country": origin.get("country")},
                                                                      "Name": origin.get("city")},
                        "HouseConsignment": {"Sequence": 1, "TotalGrossMass": gross, "ConsignmentItem": {
                            "Sequence": 1, "GoodsItemNumber": 1, "Commodity": {"Description": c.get("description")},
                            "GoodsMeasure": {"GrossMass": gross},
                            "Packaging": {"Sequence": 1, "Type": _pkg(c.get("package_type")),
                                          "Quantity": int(c.get("packages") or 1)},
                            "PreviousDocument": {"Sequence": 1, "Type": "N830", "Identification": tv.take(
                                "PreviousDocument.Identification", f"TEST-EXPORT-{shipment['reference']}",
                                "export MRN exists only after Tulli accepts the export declaration")}}}},
        "Guarantee": {"Sequence": 1, "Type": "1", "GuaranteeReference": {
            "Sequence": 1, "OtherGuaranteeReference": tv.take("Guarantee.GRN", "26FI0000000000000",
                                                             "no comprehensive guarantee until Tulli grants one"),
            "AccessCode": tv.take("Guarantee.AccessCode", "TEST", "guarantee access code issued with the GRN"),
            "Amount": {"@CurrencyUnit": "EUR", "#text": str(round((c.get("value_cents") or 100000) / 100))}}},
        "DepartureOffice": {"Identification": dep}, "DestinationOffice": {"Identification": destination_office}}
    return {"id": None, "lrn": lrn, "declaration_type": "T1", "office_of_departure": dep,
            "office_of_destination": destination_office, "national_data": {"FI015A": national}}


def ens_filing(shipment: dict, stored: dict | None, company: dict, tv: TestValues) -> dict:
    mode = shipment.get("mode") or "sea"
    ft = ENS_TYPE[mode]
    c = shipment.get("cargo") or {}
    parties = shipment.get("parties") or {}
    shipper, consignee = parties.get("shipper") or {}, parties.get("consignee") or {}
    origin, dest = shipment.get("origin") or {}, shipment.get("destination") or {}
    gross = str(c.get("weight_kg") or 0)

    def person(p):
        a = p.get("address") or {}
        return {"name": p.get("name"), "typeOfPerson": "2",
                "address": {"city": a.get("city"), "country": a.get("country"), "street": a.get("street"),
                            "postCode": a.get("postal_code")}}

    declarant = {"name": company.get("name"), "identificationNumber": company.get("eori"),
                 "address": {"city": (company.get("address") or {}).get("city"),
                             "country": (company.get("address") or {}).get("country"),
                             "street": (company.get("address") or {}).get("street"),
                             "postCode": (company.get("address") or {}).get("postal_code"), "number": "1"},
                 "communication": [{"identifier": (company.get("signatory") or {}).get("email"), "type": "EM"}]}
    hs = str(c.get("hs_hint") or "")
    if len(hs) < 6:
        items = (stored or {}).get("items") or []
        hs = str((items[0].get("hs_code") if items else "") or tv.take("goodsItem.hs", "000000", "no HS code"))
    goods = {"goodsItemNumber": 1, "commodity": {"descriptionOfGoods": c.get("description"),
                                                 "commodityCode": {"harmonizedSystemSubHeadingCode": hs[:6]}},
             "weight": {"grossMass": gross},
             "packaging": [{"typeOfPackages": _pkg(c.get("package_type")), "numberOfPackages": int(c.get("packages") or 1),
                            "shippingMarks": shipment["reference"]}]}
    routing = [{"sequenceNumber": 1, "country": origin.get("country")}, {"sequenceNumber": 2, "country": dest.get("country")}]
    house = {"containerIndicator": "1" if c.get("containers") else "0", "totalGrossMass": gross,
             "placeOfAcceptance": {"UNLOCODE": origin.get("unlocode")}, "consignee": person(consignee),
             "consignor": person(shipper), "goodsItem": [goods], "transportCharges": {"methodOfPayment": "H"},
             "placeOfDelivery": {"UNLOCODE": dest.get("unlocode")}, "countriesOfRoutingOfConsignment": routing,
             "transportDocumentHouseLevel": {"documentNumber": tv.take(
                 "transportDocumentHouseLevel.documentNumber", f"TEST-HBL-{shipment['reference']}",
                 "house bill is issued at booking with the carrier"), "type": "N714" if mode == "air" else "N703"}}
    lrn = _lrn("ics2", shipment)
    from core import config
    if not config.env("ICS2_EORI"):
        tv.take("declarant.identificationNumber", declarant["identificationNumber"],
                "ICS2_EORI (Huolitsija's own EORI) is not configured; the test tenant's EORI declares the TEST ENS")
    else:
        declarant["identificationNumber"] = config.env("ICS2_EORI")
    data = {"LRN": lrn, "documentIssueDate": {"DateTime": now_iso()[:19] + "Z"}, "declarant": declarant,
            "SpecificCircumstanceIndicator": ft[3:]}
    if mode in {"sea", "air"}:
        house.update(transportDocumentMasterLevel={"documentNumber": tv.take(
            "transportDocumentMasterLevel.documentNumber", f"TEST-MBL-{shipment['reference']}",
            "master bill comes from the carrier"), "type": "N740" if mode == "air" else "N705"},
            carrier={"identificationNumber": tv.take("carrier.identificationNumber", "TEST00000000",
                                                     "carrier not booked on this shipment")})
        data.update(addressedMemberState={"country": dest.get("country")},
                    activeBorderTransportMeans={"ModeOfTransport": MODE_CODE[mode]},
                    consignmentMasterLevel={"consignmentHouseLevel": [house]})
    else:
        raise ValueError(f"ENS TEST message for {mode} is not built by this check")
    return {"lrn": lrn, "mode": mode, "filing_type": ft, "ens_data": data}


def _pkg(t):
    from services.tulli import package_code
    return package_code(t) or "PK"


# ------------------------------------------------------------------------------------------------- the check
def _schema_errors(exc: Exception) -> list[str]:
    log = getattr(exc, "error_log", None) or getattr(getattr(exc, "result", None), "errors", None)
    if log:
        return [str(e)[:300] for e in list(log)[:8]]
    return [f"{type(exc).__name__}: {exc}"[:600]]


def _build(system: str, decl: dict) -> tuple[bytes, str, str]:
    """-> (xml, message name, schema label); raises when the official schema refuses the message."""
    if system == "import":
        xml = tulli_import.build_message(decl, "TEST")
        tulli_import.validate(xml)
        return xml.encode(), "FI415B", "Tulli import MIG 2.1.7 / XSD 3.0.37 (v1.21 package)"
    if system == "export":
        xml = tulli_export.build_message(decl, "TEST")
        tulli_export.validate(xml)
        return xml.encode(), "FI515A", "Tulli export MIG 1.1.1 / XSD 1.0.90 (v1.21 package)"
    if system == "transit":
        xml = ncts_fi.build("FI015A", decl["national_data"]["FI015A"])
        schema_validate(xml, ncts_fi.DIRECTORY / "FI015A.xsd")
        return xml.encode(), "FI015A", "Tulli NCTS P6 national MIG 2.2.0 (FI015A.xsd)"
    xml = ics2.build(decl["filing_type"], decl["ens_data"])  # build_ens would put the unset ICS2_EORI in
    schema_validate(xml, ics2.path(decl["filing_type"]))
    return xml.encode(), decl["filing_type"], f"EU ICS2 {decl['filing_type']} XSD 3.2.0.0"


def _ics2_envelope(payload: bytes, lrn: str) -> dict:
    env = ics2_gateway.envelope("submitRequest", payload.decode(), f"TEST-{lrn}@huolitsija")
    root = etree.fromstring(env)
    ok = root.find(".//{%s}UserMessage" % ics2_gateway.EB) is not None and \
        root.find(".//{%s}submitRequest" % ics2_gateway.WS) is not None
    return {"ok": ok, "envelope": "eDelivery AS4 ebMS3 UserMessage via the access point's WS plugin (ICD 1.17)",
            "signing": "the AS4 access point signs and encrypts; Tulli v1.21 carries no ICS2 messages",
            "bytes": len(env), "sent": False}


async def check_system(system: str, shipment: dict, company: dict, stored: dict | None, eori_cache: dict) -> dict:
    from services.kyc import eori_check
    tv = TestValues()
    out = {"system": system, "shipment_ref": shipment.get("reference"), "test": True, "sent": False}
    try:
        decl = {"import": lambda: import_declaration(shipment, stored, company, tv),
                "export": lambda: export_declaration(shipment, company, tv),
                "transit": lambda: transit_declaration(shipment, company, tv),
                "ics2": lambda: ens_filing(shipment, stored, company, tv)}[system]()
        out["lrn"] = decl["lrn"]
        if stored:
            out["based_on_declaration"] = {"id": stored.get("id"), "lrn": stored.get("lrn")}
        payload, message, schema = await asyncio.to_thread(_build, system, decl)
        out.update(business_message=message, schema=schema, business_xml_valid=True, payload_bytes=len(payload),
                   payload_sha256=hashlib.sha256(payload).hexdigest())
    except (ValueError, KeyError, etree.DocumentInvalid, etree.XMLSyntaxError) as exc:
        out.update(ok=False, business_xml_valid=False, errors=_schema_errors(exc), test_values=tv)
        return out
    if system in TULLI_APPLICATION:
        env = await asyncio.to_thread(ready.envelope_self_test, payload, TULLI_APPLICATION[system], message)
        out["envelope"] = {k: env.get(k) for k in ("ok", "application", "application_request", "soap_upload",
                                                   "signing_key", "error", "sent")}
        out["envelope"]["channel"] = "Tulli Direct Message Exchange v1.21"
    else:
        out["envelope"] = await asyncio.to_thread(_ics2_envelope, payload, decl["lrn"])
    root = etree.fromstring(payload)
    eoris = sorted({e.text.strip() for e in root.iter() if isinstance(e.tag, str) and e.text and
                    etree.QName(e).localname in ("Identification", "identificationNumber")
                    and e.text.strip()[:2].isalpha() and e.text.strip()[:2].upper() in EU | {"XI"}
                    and any(ch.isdigit() for ch in e.text) and len(e.text.strip()) <= 17
                    and not e.text.strip().upper().startswith(("TEST", "FI00", "26FI"))
                    and not etree.QName(e.getparent()).localname.endswith("Office")})
    checks = []
    for eori in eoris:
        if eori not in eori_cache:
            eori_cache[eori] = await eori_check(eori)
        r = eori_cache[eori]
        checks.append({"eori": eori, "status": r.get("status"), "valid": r.get("valid"), "summary": r.get("summary"),
                       "source": r.get("source"), "url": r.get("url"), "checked_at": r.get("checked_at")})
    out["eos_check"] = {"read_only": True, "service": "EU EOS EORI validation (validateEORI)", "results": checks}
    out["test_values"] = tv
    out["ok"] = bool(out["business_xml_valid"] and out["envelope"].get("ok"))
    out["can_send"] = False
    return out


async def run(shipment_id: str) -> dict:
    shipment = await db.raw.shipments.find_one({"id": shipment_id}, NO_ID)
    if not shipment:
        raise LookupError("shipment")
    company = await db.raw.companies.find_one({"id": shipment.get("company_id")}, NO_ID) or {}
    if not (company.get("test_tenant") or company.get("test")):
        raise PermissionError("Only a test tenant's TEST shipment can be used for the TEST message check")
    systems = systems_for(shipment)
    stored = {}
    if "import" in systems:
        stored["import"] = await db.raw.customs_declarations.find_one(
            {"shipment_id": shipment_id, "status": {"$nin": ["released", "cancelled"]}}, NO_ID, sort=[("created_at", -1)])
        stored["ics2"] = stored["import"]
    cache: dict = {}
    results = [await check_system(s, shipment, company, stored.get(s), cache) for s in systems]
    return {"checked_at": now_iso(), "shipment": {"id": shipment_id, "reference": shipment.get("reference"),
                                                  "company_id": shipment.get("company_id"),
                                                  "route": f"{(shipment.get('origin') or {}).get('unlocode')} → "
                                                           f"{(shipment.get('destination') or {}).get('unlocode')}",
                                                  "mode": shipment.get("mode")},
            "systems": results, "sent": False,
            "note": "TEST messages: built, schema-validated and enveloped in memory; never stored or sent to an authority."}
