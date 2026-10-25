"""s14 EDI: EDIFACT syntax, IFTMIN reading, IFTMCS/IFTSTA writing, DCSA JSON — no server needed.

Independent checks (not our own code agreeing with itself):
- the IFTMIN fixture is hand-written EDIFACT (release characters, line breaks, D.04A segments);
- our outbound interchanges are re-read with pydifact (a separate EDIFACT library) and their UNT/UNZ counts recounted;
- DCSA events are validated against DCSA's published OpenAPI schemas (DCSA_TNT 2.2.0 + domains, copied from SwaggerHub
  into tests/fixtures/edi/).
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

from services.edi import codes, dcsa
from services.edi.edifact import InterchangeError, Segment, parse, segment_text, write_interchange
from services.edi.iftmin import parse_iftmin
from services.edi.outbound import iftmcs, iftsta

FIX = Path(__file__).parent / "fixtures" / "edi"


def fixture_iftmin(ready="20261102") -> str:
    return (FIX / "iftmin_road_tll_tre.edi").read_text().replace("{READY}", ready)


def recount(raw: str) -> list[tuple[int, int]]:
    """(declared, actual) UNT counts, counted with plain string splitting on unreleased terminators."""
    segs, buf, i = [], "", 0
    while i < len(raw):
        ch = raw[i]
        if ch == "?" and i + 1 < len(raw):
            buf += raw[i:i + 2]
            i += 2
            continue
        if ch == "'":
            segs.append(buf.strip())
            buf = ""
        else:
            buf += ch
        i += 1
    out, n = [], None
    for s in segs:
        if s.startswith("UNH+"):
            n = 1
        elif s.startswith("UNT+"):
            out.append((int(s.split("+")[1]), n + 1))
            n = None
        elif n is not None:
            n += 1
    return out


# ------------------------------------------------------------------------------------------- syntax

def test_fixture_envelope_and_release_characters():
    ic = parse(fixture_iftmin())
    assert (ic.syntax, ic.sender, ic.recipient, ic.control_ref) == ("UNOC", "KALUSTE1", "HUOLITSIJA", "KA000417")
    m = ic.messages[0]
    assert (m.type, m.version, m.release, m.agency) == ("IFTMIN", "D", "04A", "UN")
    assert len(m.segments) + 2 == 28  # UNT+28
    ftx = m.first("FTX", e1="AAI")
    assert ftx.el(4, 1) == "Call 30 min before arrival: dock 3. Fragile's glass tops"  # ?: and ?' released
    com = m.first("COM")
    assert com.el(1, 1) == "+372 5550 1234" and com.el(1, 2) == "TE"  # ?+ released


@pytest.mark.parametrize("mutate,err", [
    (lambda s: s.replace("UNT+28+1", "UNT+27+1"), "UNT says 27"),
    (lambda s: s.replace("UNZ+1+KA000417", "UNZ+1+KA000999"), "UNZ control reference"),
    (lambda s: s.replace("UNZ+1+KA000417", "UNZ+2+KA000417"), "UNZ says 2 messages"),
    (lambda s: s.replace("UNT+28+1", "UNT+28+9"), "UNT reference 9"),
    (lambda s: s.rstrip().rstrip("'"), "segment terminator"),
])
def test_envelope_errors_are_rejected(mutate, err):
    with pytest.raises(InterchangeError, match=err):
        parse(mutate(fixture_iftmin()))


def test_custom_una_delimiters():
    raw = ("UNA|*,! ~UNB*UNOC|4*A|ZZZ*B|ZZZ*261024|1200*7~UNH*1*IFTMIN|D|04A|UN~"
           "FTX*AAI***pipe!| star!* tilde!~ bang!!~UNT*3*1~UNZ*1*7~")
    m = parse(raw).messages[0]
    assert m.first("FTX").el(4, 1) == "pipe| star* tilde~ bang!"


def test_writer_escapes_and_counts_round_trip():
    tricky = "Rauta+Puu Oy: 'Kulma' ?50%"
    raw = write_interchange("HUOLITSIJA", "PARTNER", [("IFTSTA", "D", "04A", [("BGM", ["23", "X1", "9"]),
                                                                            ("FTX", ["AAI", "", "", [tricky]])])],
                            control_ref="00000042", test=True)
    assert "?+" in raw and "?:" in raw and "?'" in raw and "??" in raw
    ic = parse(raw)
    assert ic.test is True and ic.control_ref == "00000042"
    assert ic.messages[0].first("FTX").el(4, 1) == tricky
    assert recount(raw) == [(4, 4)]
    assert segment_text(Segment("LOC", ["5", ["FIHEL", "139", "6"]])) == "LOC+5+FIHEL:139:6'"


# ------------------------------------------------------------------------------------------- IFTMIN

def test_iftmin_to_instruction():
    instr = parse_iftmin(parse(fixture_iftmin()).messages[0])
    assert instr["errors"] == []
    assert instr["doc_number"] == "KA-BOOK-2026-0417" and instr["function"] == "9" and instr["doc_code"] == "610"
    assert instr["mode"] == "road" and instr["ready_date"] == "2026-11-02"
    assert instr["origin"]["unlocode"] == "EETLL" and instr["destination"]["unlocode"] == "FITMP"
    assert (instr["incoterm"], instr["incoterm_place"]) == ("FCA", "Tallinn")
    s = instr["parties"]["shipper"]
    assert s["name"] == "Tallinna Mööbel OÜ" and s["address"] == {"street": "Pärnu mnt 139", "city": "Tallinn", "region": None,
                                                                   "postal_code": "11317", "country": "EE"}
    assert s["contact"]["email"] == "kadri.tamm@moobel.example"
    assert instr["parties"]["consignee"]["address"]["city"] == "Tampere"
    assert instr["references"] == {"customer_ref": "KA-PO-778812", "po_numbers": ["PO-4410", "PO-4411"], "other": {}}
    c = instr["cargo"]
    assert (c["weight_kg"], c["volume_m3"], c["packages"], c["package_type"], c["load_type"]) == (600.0, 2.4, 2, "pallet_eur", "loose")
    assert c["description"] == "Oak dining tables, flat-packed"


def test_iftmin_missing_parts_are_named():
    raw = fixture_iftmin().replace("NAD+CN+++Testi Oy+Hatanpään valtatie 24+Tampere++33100+FI'\n", "") \
        .replace("CNT+7:600:KGM'\n", "").replace("MEA+AAE+AAB+KGM:600'\n", "").replace("UNT+28+1", "UNT+25+1")
    instr = parse_iftmin(parse(raw).messages[0])
    assert any("consignee" in e for e in instr["errors"])
    assert any("gross weight" in e for e in instr["errors"])


def test_iftmin_containers():
    raw = fixture_iftmin().replace("DIM+1+CMT:120:80:125'\n", "DIM+1+CMT:120:80:125'\nEQD+CN++45G1:102:5'\nEQN+2'\n") \
        .replace("UNT+28+1", "UNT+30+1")
    instr = parse_iftmin(parse(raw).messages[0])
    assert instr["cargo"]["load_type"] == "FCL" and instr["cargo"]["containers"] == [{"type": "40HC", "count": 2}]


# ------------------------------------------------------------------------------------------- outbound, checked by pydifact

def _pydifact(raw: str):
    from pydifact.segmentcollection import Interchange

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return Interchange.from_str(raw)


SHIPMENT = {"id": "s1", "reference": "HUO-2026-000123", "company_id": "c1", "mode": "road",
            "origin": {"unlocode": "EETLL", "name": "Tallinn"}, "destination": {"unlocode": "FITMP", "name": "Tampere"},
            "etd": "2026-11-02T08:00:00+00:00", "eta": "2026-11-03T14:00:00+00:00",
            "references": {"customer_ref": "KA-PO-778812", "po_numbers": ["PO-4410"]},
            "legs": [{"seq": 1, "mode": "road", "role": "main", "carrier": "Tallink Cargo"}],
            "cargo": {"containers": []}}


def test_iftmcs_accepted_is_valid_edifact():
    instr = parse_iftmin(parse(fixture_iftmin()).messages[0])
    segs = iftmcs(instruction=instr, function=codes.FN_ACCEPTED, our_ref="HUO-2026-000123", shipment=SHIPMENT,
                  option={"total_cents": 41250, "carrier_name": "Tallink Cargo"}, reasons=None,
                  doc_date_iso="2026-10-24T10:20:00+00:00", our_party={"name": "Huolitsija Oy", "unb_id": "HUOLITSIJA"})
    raw = write_interchange("HUOLITSIJA", "KALUSTE1", [("IFTMCS", "D", "04A", segs)], control_ref="00000001")
    assert recount(raw) == [(len(segs) + 2, len(segs) + 2)]
    ic = _pydifact(raw)
    msgs = list(ic.get_messages())
    assert len(msgs) == 1 and msgs[0].type == "IFTMCS"
    tags = {s.tag: s.elements for s in msgs[0].segments}
    assert tags["BGM"] == ["770", "HUO-2026-000123", "29"]
    rffs = [s.elements[0] for s in msgs[0].segments if s.tag == "RFF"]
    assert ["ACW", "KA-BOOK-2026-0417"] in rffs and ["BN", "HUO-2026-000123"] in rffs and ["CU", "KA-PO-778812"] in rffs
    dtms = [s.elements[0] for s in msgs[0].segments if s.tag == "DTM"]
    assert ["133", "202611020800", "203"] in dtms and ["132", "202611031400", "203"] in dtms
    assert ["64", "412.50"] in [s.elements[0] for s in msgs[0].segments if s.tag == "MOA"]


def test_iftmcs_not_accepted_carries_reasons():
    instr = parse_iftmin(parse(fixture_iftmin()).messages[0])
    segs = iftmcs(instruction=instr, function=codes.FN_NOT_ACCEPTED, our_ref=None, shipment=None, option=None,
                  reasons=["Bookings open once your company is verified."], doc_date_iso="2026-10-24T10:20:00+00:00",
                  our_party={"name": "Huolitsija Oy", "unb_id": "HUOLITSIJA"})
    raw = write_interchange("HUOLITSIJA", "KALUSTE1", [("IFTMCS", "D", "04A", segs)], control_ref="00000002")
    m = parse(raw).messages[0]
    assert m.first("BGM").el(3) == "27" and m.first("BGM").el(2) == "KA-BOOK-2026-0417"
    assert "verified" in m.first("FTX").el(4, 1)


def test_iftsta_status_codes():
    events = [{"code": "picked_up", "at": "2026-11-02T08:05:00+00:00", "label": "Picked up", "location": {"unlocode": "EETLL", "name": "Tallinn"}, "leg_seq": 1},
              {"code": "delivered", "at": "2026-11-03T13:40:00+00:00", "label": "Delivered", "location": {"name": "Tampere"}}]
    for ev, (code, text) in zip(events, [("13", "Collection/pick-up, completed"), ("21", "Delivery, completed")]):
        segs = iftsta(shipment=SHIPMENT, events=[ev], doc_number="D1", doc_date_iso="2026-11-03T13:41:00+00:00",
                      our_party={"name": "Huolitsija Oy", "unb_id": "HUOLITSIJA"})
        raw = write_interchange("HUOLITSIJA", "KALUSTE1", [("IFTSTA", "D", "04A", segs)], control_ref="00000003")
        msg = list(_pydifact(raw).get_messages())[0]
        sts = [s.elements for s in msg.segments if s.tag == "STS"][0]
        assert sts == ["1", [code, "", "6", text]]
        assert ["CNI", ["1", "HUO-2026-000123"]] == [[s.tag, s.elements] for s in msg.segments if s.tag == "CNI"][0]
        assert any(s.tag == "DTM" and s.elements[0][0] == "334" for s in msg.segments)
    # partner override of a code keeps our text
    segs = iftsta(shipment=SHIPMENT, events=[events[1]], doc_number="D2", doc_date_iso="2026-11-03T13:41:00+00:00",
                  our_party={"name": "Huolitsija Oy", "unb_id": "HUOLITSIJA"}, code_map={"delivered": "DEL"})
    assert ("STS", ["1", ["DEL", "", "6", "Delivery, completed"]]) in segs


# ------------------------------------------------------------------------------------------- DCSA

def _dcsa_validator(schema_name: str):
    from jsonschema import Draft7Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT7

    files = {"https://api.swaggerhub.com/domains/dcsaorg/EVENT_DOMAIN/1.0.4": "dcsa_event_domain_1.0.4.json",
             "https://api.swaggerhub.com/domains/dcsaorg/DCSA_DOMAIN/1.0.3": "dcsa_domain_1.0.3.json",
             "https://api.swaggerhub.com/domains/dcsaorg/LOCATION_DOMAIN/1.0.3": "dcsa_location_domain_1.0.3.json",
             "https://api.swaggerhub.com/domains/dcsaorg/DOCUMENTATION_DOMAIN/1.0.4": "dcsa_documentation_domain_1.0.4.json"}
    resources = []
    for uri, name in files.items():
        text = (FIX / name).read_text()
        # Known defect in EVENT_DOMAIN 1.0.4: the documentReferenceType enum lists "BKG (Booking)" / "TRD (Transport
        # Document)" (labels with the code); DCSA's reference implementation and the description use the bare code.
        text = text.replace('"BKG (Booking)"', '"BKG"').replace('"TRD (Transport Document)"', '"TRD"')
        resources.append((uri, Resource.from_contents(json.loads(text), default_specification=DRAFT7)))
    registry = Registry().with_resources(resources)
    schema = {"$ref": f"https://api.swaggerhub.com/domains/dcsaorg/EVENT_DOMAIN/1.0.4#/components/schemas/{schema_name}"}
    return Draft7Validator(schema, registry=registry)


def test_dcsa_events_validate_against_published_schemas():
    sea = {**SHIPMENT, "mode": "sea", "legs": [{"seq": 1, "mode": "sea", "role": "main", "carrier": "MSC",
                                                "vessel": {"imo": "9454448", "name": "MSC Example"}}],
           "cargo": {"containers": [{"type": "40HC", "count": 1}]}}
    tevs = [{"id": "e1", "code": "booked", "at": "2026-10-24T10:20:00+00:00"},
            {"id": "e2", "code": "picked_up", "at": "2026-11-02T08:05:00+00:00", "location": {"name": "Tallinn", "lat": 59.43, "lon": 24.75}},
            {"id": "e3", "code": "departed", "at": "2026-11-02T18:00:00+00:00", "location": {"unlocode": "EETLL", "name": "Tallinn"}, "leg_seq": 1},
            {"id": "e4", "code": "customs_released", "at": "2026-11-03T09:00:00+00:00"},
            {"id": "e5", "code": "delivered", "at": "2026-11-03T13:40:00+00:00", "location": {"name": "Tampere"}},
            {"id": "e6", "code": "exception", "at": "2026-11-03T10:00:00+00:00"}]
    events = dcsa.tnt_events(sea, tevs)
    assert [e["eventType"] for e in events] == ["SHIPMENT", "EQUIPMENT", "TRANSPORT", "SHIPMENT", "EQUIPMENT"]  # exception: no DCSA code
    schema_for = {"SHIPMENT": "shipmentEvent", "TRANSPORT": "transportEvent", "EQUIPMENT": "equipmentEvent"}
    for e in events:
        errs = sorted(_dcsa_validator(schema_for[e["eventType"]]).iter_errors(e), key=str)
        assert not errs, [f"{list(x.path)}: {x.message}" for x in errs]
    tr = events[2]
    assert tr["transportEventTypeCode"] == "DEPA" and tr["transportCall"]["modeOfTransport"] == "VESSEL"
    assert tr["transportCall"]["UNLocationCode"] == "EETLL"
    assert events[1]["ISOEquipmentCode"] == "45G1" and events[1]["equipmentEventTypeCode"] == "PICK"
    # a wrong code must fail the schema (the validator is really checking)
    bad = {**events[2], "transportEventTypeCode": "LEFT"}
    assert list(_dcsa_validator("transportEvent").iter_errors(bad))


def test_dcsa_booking_request_to_instruction():
    body = {"carrierBookingRequestReference": "BKR-7781", "expectedDepartureDate": "2026-11-10", "incoTerms": "FOB",
            "commodities": [{"commodityType": "Furniture", "HSCodes": ["940360"], "cargoGrossWeight": 12000, "cargoGrossWeightUnit": "KGM"}],
            "requestedEquipments": [{"ISOEquipmentCode": "45G1", "units": 1, "isShipperOwned": False}],
            "shipmentLocations": [{"shipmentLocationTypeCode": "POL", "location": {"UNLocationCode": "CNSHA"}},
                                  {"shipmentLocationTypeCode": "POD", "location": {"UNLocationCode": "FIHEL"}}],
            "documentParties": [{"partyFunction": "OS", "party": {"partyName": "Shanghai Furniture Co", "address": {
                "street": "Pudong Ave", "streetNumber": "100", "city": "Shanghai", "postCode": "200120", "countryCode": "CN"}}},
                {"partyFunction": "CN", "party": {"partyName": "Kaluste Oy", "address": {"street": "Mannerheimintie", "streetNumber": "5",
                                                                                         "city": "Helsinki", "postCode": "00100", "countryCode": "FI"}}}]}
    instr = dcsa.booking_to_instruction(body)
    assert instr["errors"] == []
    assert instr["origin"]["unlocode"] == "CNSHA" and instr["destination"]["unlocode"] == "FIHEL" and instr["mode"] == "sea"
    assert instr["cargo"]["containers"] == [{"type": "40HC", "count": 1}] and instr["cargo"]["hs_hint"] == "940360"
    assert instr["parties"]["shipper"]["address"]["street"] == "Pudong Ave 100"
    del body["documentParties"]
    assert any("shipper" in e for e in dcsa.booking_to_instruction(body)["errors"])


def test_iftmin_dangerous_goods_segment():
    raw = fixture_iftmin().replace("DIM+1+CMT:120:80:125'\n",
                                   "DIM+1+CMT:120:80:125'\nDGS+ADR+3+1263++3'\nFTX+AAD+++PAINT'\nCOM+?+372 5550 9999:TE'\n") \
        .replace("UNT+28+1", "UNT+31+1")
    dg = parse_iftmin(parse(raw).messages[0])["cargo"]["dg"]
    assert dg == {"un_number": "1263", "class": "3", "packing_group": "III", "regulation": "ADR",
                  "proper_shipping_name": "PAINT", "emergency_phone": "+372 5550 9999"}
