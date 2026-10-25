"""Outbound EDIFACT D.04A messages: IFTMCS (booking confirmation / status of an instruction) and IFTSTA (status).

Both return the segment list for edifact.write_interchange; the service wraps them in UNB/UNZ per partner."""
from __future__ import annotations

from services.edi import codes
from services.edi.edifact import clean, edi_datetime


def _nad(qual: str, party: dict | None, *, party_id: str | None = None) -> tuple | None:
    if not party and not party_id:
        return None
    party = party or {}
    a = party.get("address") or {}
    return ("NAD", [qual, [party_id or "", "", "ZZZ" if party_id else ""], "", [clean(party.get("name"), 35)],
                    [clean(a.get("street"), 35)], clean(a.get("city"), 35), "", clean(a.get("postal_code"), 17),
                    clean(a.get("country"), 3)])


def _loc(qual: str, place: dict | None) -> tuple | None:
    if not place:
        return None
    code = place.get("unlocode") or ""
    name = place.get("name") or place.get("city") or ""
    if not (code or name):
        return None
    return ("LOC", [qual, [code, "139" if code else "", "6" if code else "", clean(name, 256)]])


def iftmcs(*, instruction: dict, function: str, our_ref: str | None, shipment: dict | None, option: dict | None,
           reasons: list[str] | None, doc_date_iso: str, our_party: dict) -> list[tuple]:
    """Answer to an IFTMIN. function: 29 accepted, 23 on hold (owner approval), 27 not accepted, 1 cancellation done."""
    seg: list[tuple | None] = [
        ("BGM", [codes.DOC_BOOKING_CONFIRMATION, clean(our_ref or instruction.get("doc_number"), 35), function]),
        ("DTM", [["137", edi_datetime(doc_date_iso), "203"]]),
    ]
    for r in reasons or []:
        # FTX 4440 is an..512 per occurrence, 5 occurrences; one FTX per reason
        text = clean(r, 512)
        seg.append(("FTX", ["AAI", "", "", [text[i:i + 512] for i in range(0, len(text), 512)][:5]]))
    if option:
        seg.append(("CUX", [["2", "EUR", "4"]]))
        seg.append(("MOA", [["64", f"{(option.get('total_cents') or 0) / 100:.2f}"]]))  # 64 = freight charge
    # References back to the instruction (SG3 RFF-DTM)
    seg.append(("RFF", [["ACW", clean(instruction.get("doc_number"), 70)]]))  # reference to previous message
    if instruction.get("references", {}).get("customer_ref"):
        seg.append(("RFF", [["CU", clean(instruction["references"]["customer_ref"], 70)]]))
    if shipment:
        seg.append(("RFF", [["BN", clean(shipment.get("reference"), 70)]]))  # our booking reference
    for po in (instruction.get("references") or {}).get("po_numbers") or []:
        seg.append(("RFF", [["ON", clean(po, 70)]]))
    # Transport (SG8 TDT-LOC-DTM)
    if option or shipment:
        mode = (shipment or {}).get("mode") or (option or {}).get("mode") or instruction.get("mode")
        carrier = (option or {}).get("carrier_name") or ""
        seg.append(("TDT", ["20", "", [codes.MODE_TO_REC19.get(mode or "", "9")], "", ["", "", "", clean(carrier, 35)]]))
        origin = (shipment or {}).get("origin") or instruction.get("origin")
        destination = (shipment or {}).get("destination") or instruction.get("destination")
        seg.append(_loc("9", origin))
        etd = (shipment or {}).get("etd") or (option or {}).get("etd")
        if etd:
            seg.append(("DTM", [["133", edi_datetime(etd), "203"]]))
        seg.append(_loc("11", destination))
        eta = (shipment or {}).get("eta") or (option or {}).get("eta")
        if eta:
            seg.append(("DTM", [["132", edi_datetime(eta), "203"]]))
    # Parties (SG11 NAD)
    seg.append(_nad("FW", our_party, party_id=our_party.get("unb_id")))
    parties = instruction.get("parties") or {}
    seg.append(_nad("CZ", parties.get("shipper")))
    seg.append(_nad("CN", parties.get("consignee")))
    return [s for s in seg if s]


def iftsta(*, shipment: dict, events: list[dict], doc_number: str, doc_date_iso: str, our_party: dict,
           code_map: dict | None = None) -> list[tuple]:
    """Status report for one shipment (one CNI) with one STS group per event."""
    seg: list[tuple | None] = [
        ("BGM", [codes.DOC_STATUS_INFORMATION, clean(doc_number, 35), codes.FN_ORIGINAL]),
        ("DTM", [["137", edi_datetime(doc_date_iso), "203"]]),
        _nad("FW", our_party, party_id=our_party.get("unb_id")),
        ("CNI", ["1", clean(shipment.get("reference"), 35)]),
    ]
    refs = shipment.get("references") or {}
    for ev in events:
        code, text = codes.STATUS_CODES[ev["code"]]
        if (code_map or {}).get(ev["code"]):
            code = str(code_map[ev["code"]])  # partner MIG override; the text stays ours
        seg.append(("STS", ["1", [code, "", "6", clean(text, 35)]]))
        seg.append(("RFF", [["BN", clean(shipment.get("reference"), 70)]]))
        if refs.get("customer_ref"):
            seg.append(("RFF", [["CU", clean(refs["customer_ref"], 70)]]))
        seg.append(("DTM", [["334", edi_datetime(ev.get("at")), "203"]]))
        if ev.get("note") or ev.get("label"):
            seg.append(("FTX", ["AAI", "", "", [clean(ev.get("note") or ev.get("label"), 512)]]))
        seg.append(_loc("175", ev.get("location")))
        leg = next((lg for lg in shipment.get("legs") or [] if lg.get("seq") == ev.get("leg_seq")), None)
        if leg:
            seg.append(("TDT", ["20", "", [codes.MODE_TO_REC19.get(leg.get("mode") or "", "9")], "",
                                ["", "", "", clean(leg.get("carrier") or leg.get("carrier_name"), 35)]]))
    return [s for s in seg if s]
