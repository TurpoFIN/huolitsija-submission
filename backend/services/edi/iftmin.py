"""IFTMIN (instruction message, D.04A) → booking instruction.

    instr = parse_iftmin(message)   # edifact.Message of type IFTMIN

Returns a plain dict the service turns into a quote and a booking through the portal's own code:
    {doc_number, doc_code, function, doc_date, mode, origin{unlocode,name,country}, destination{...}, ready_date,
     delivery_date, incoterm, incoterm_place, carrier_name, parties{shipper, consignee, notify, forwarder},
     references{customer_ref, po_numbers[], other{}}, cargo{...}, instructions, errors[]}
`errors` lists what is missing for a booking (plain sentences); the service answers those with IFTMCS "not accepted".
"""
from __future__ import annotations

from services.edi import codes
from services.edi.edifact import Message, Segment, parse_edi_datetime

PARTY_ROLES = {"CZ": "shipper", "SH": "shipper", "CN": "consignee", "NI": "notify", "N1": "notify", "FW": "forwarder"}


def _num(v: str) -> float | None:
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _weight_kg(value: str, unit: str) -> float | None:
    n = _num(value)
    if n is None:
        return None
    return {"KGM": n, "TNE": n * 1000, "LBR": n * 0.45359237, "GRM": n / 1000}.get((unit or "KGM").upper(), n)


def _volume_m3(value: str, unit: str) -> float | None:
    n = _num(value)
    if n is None:
        return None
    return {"MTQ": n, "LTR": n / 1000, "DMQ": n / 1000, "FTQ": n * 0.0283168}.get((unit or "MTQ").upper(), n)


def _dtm(seg: Segment) -> tuple[str, str | None]:
    return seg.el(1, 1), parse_edi_datetime(seg.el(1, 2), seg.el(1, 3) or "102")


def _loc(seg: Segment) -> dict:
    code = seg.el(2, 1).strip().upper()
    name = seg.el(2, 4) or None
    out = {"unlocode": code if len(code) == 5 and code.isalnum() else None, "name": name,
           "country": code[:2] if len(code) == 5 else None}
    if not out["unlocode"]:
        out["code"] = code or None
    return out


def _party(group: list[Segment]) -> dict:
    nad = group[0]
    name_parts = [p for p in nad.comps(4) if p] or [p for p in nad.comps(3) if p]
    street = " ".join(p for p in nad.comps(5) if p)
    party = {"name": " ".join(name_parts) or None,
             "id": nad.el(2, 1) or None, "id_agency": nad.el(2, 3) or None,
             "address": {"street": street or None, "city": nad.el(6) or None, "region": nad.el(7) or None,
                         "postal_code": nad.el(8) or None, "country": (nad.el(9) or "").upper() or None}}
    contact: dict = {}
    for s in group[1:]:
        if s.tag == "CTA":
            contact["name"] = s.el(2, 2) or s.el(2, 1) or None
        elif s.tag == "COM":
            kind = s.el(1, 2).upper()
            if kind == "TE":
                contact["phone"] = s.el(1, 1)
            elif kind == "EM":
                contact["email"] = s.el(1, 1)
        elif s.tag == "RFF" and s.el(1, 1) in ("VA", "AHP"):
            party["vat_id"] = s.el(1, 2)
        elif s.tag == "RFF" and s.el(1, 1) in ("ACD", "EOR", "AVS"):
            party["eori"] = s.el(1, 2)
    if contact:
        party["contact"] = contact
    return party


def parse_iftmin(msg: Message) -> dict:
    errors: list[str] = []
    bgm = msg.first("BGM")
    out: dict = {
        "message_ref": msg.ref, "doc_code": bgm.el(1, 1) if bgm else None, "doc_number": bgm.el(2, 1) if bgm else None,
        "function": (bgm.el(3) if bgm else "") or codes.FN_ORIGINAL, "doc_date": None, "mode": None, "origin": None,
        "destination": None, "ready_date": None, "delivery_date": None, "incoterm": None, "incoterm_place": None,
        "carrier_name": None, "parties": {}, "references": {"customer_ref": None, "po_numbers": [], "other": {}},
        "cargo": {}, "instructions": None, "errors": errors,
    }
    if not bgm or not out["doc_number"]:
        errors.append("BGM with the document number is missing.")
    # Header DTM (before the first group trigger)
    texts: list[str] = []
    for s in msg.segments:
        if s.tag in ("NAD", "GID", "EQD", "TDT"):
            break
        if s.tag == "DTM":
            q, v = _dtm(s)
            if q == "137":
                out["doc_date"] = v
            elif q in ("200", "234", "10"):  # pick-up/collection, collection date, shipment date requested
                out["ready_date"] = (v or "")[:10] or None
            elif q in ("2", "17", "64"):  # delivery requested / estimated / earliest
                out["delivery_date"] = (v or "")[:10] or None
        elif s.tag == "FTX" and s.el(1) in ("AAI", "DEL", "AIN", "PRD", "SSR"):
            texts.append(" ".join(p for p in s.comps(4) if p))
        elif s.tag == "TOD":
            term = (s.el(3, 1) or "").upper()
            if term in codes.INCOTERMS:
                out["incoterm"] = term
                out["incoterm_place"] = s.el(3, 4) or s.el(3, 5) or None
        elif s.tag == "RFF":
            q, v = s.el(1, 1), s.el(1, 2)
            if q in ("CU", "SI", "FF") and not out["references"]["customer_ref"]:
                out["references"]["customer_ref"] = v
            elif q in ("ON", "CO", "VN"):
                out["references"]["po_numbers"].append(v)
            elif q:
                out["references"]["other"][q] = v
        elif s.tag == "CNT":
            q, v, unit = s.el(1, 1), s.el(1, 2), s.el(1, 3)
            if q == "7":
                out["cargo"]["weight_kg"] = _weight_kg(v, unit)
            elif q in ("11", "16"):  # number of packages
                n = _num(v)
                out["cargo"]["packages"] = int(n) if n else None
            elif q == "15":
                out["cargo"]["volume_m3"] = _volume_m3(v, unit)
        elif s.tag == "LOC":
            _apply_loc(out, s)
    # Transport (TDT group: TDT, LOC, DTM)
    for g in msg.groups("TDT", stop={"NAD", "GID", "EQD"}):
        tdt = g[0]
        if tdt.el(1) not in ("20", "10", ""):  # main carriage (20) or pre-carriage booking (10)
            continue
        mode = codes.REC19_TO_MODE.get(tdt.el(3, 1))
        out["mode"] = out["mode"] or mode
        out["carrier_name"] = out["carrier_name"] or tdt.el(5, 4) or None
        for s in g[1:]:
            if s.tag == "LOC":
                _apply_loc(out, s)
            elif s.tag == "DTM":
                q, v = _dtm(s)
                if q in ("133", "200") and not out["ready_date"]:
                    out["ready_date"] = (v or "")[:10] or None
                elif q in ("132", "2") and not out["delivery_date"]:
                    out["delivery_date"] = (v or "")[:10] or None
    # Parties
    for g in msg.groups("NAD", stop={"GID", "EQD", "TDT"}):
        role = PARTY_ROLES.get(g[0].el(1))
        if role and role not in out["parties"]:
            out["parties"][role] = _party(g)
    # Goods
    descriptions, containers = [], {}
    dims, dg = [], None
    pkg_type = None
    for g in msg.groups("GID", stop={"EQD"}):
        gid = g[0]
        n, kind = _num(gid.el(2, 1)), gid.el(2, 2).upper()
        if n and not out["cargo"].get("packages_from_gid"):
            out["cargo"]["packages_from_gid"] = 0
        if n:
            out["cargo"]["packages_from_gid"] += int(n)
        pkg_type = pkg_type or codes.PACKAGE_TYPES.get(kind, "other" if kind else None)
        for s in g[1:]:
            if s.tag == "FTX" and s.el(1) in ("AAA", "AAI"):
                descriptions.append(" ".join(p for p in s.comps(4) if p))
            elif s.tag == "PIA":
                hs = s.el(2, 1)
                if hs and s.el(2, 2) in ("HS", ""):
                    out["cargo"]["hs_hint"] = hs
            elif s.tag == "MEA" and s.el(1) in ("AAE", "WT", "VOL"):
                what, unit, val = s.el(2, 1), s.el(3, 1), s.el(3, 2)
                if what in ("AAB", "G", "WT") and unit in ("KGM", "TNE", "LBR", "GRM"):
                    out["cargo"].setdefault("_gid_weight", 0.0)
                    out["cargo"]["_gid_weight"] += _weight_kg(val, unit) or 0
                elif what in ("AAW", "ABJ", "VOL") or unit in ("MTQ", "LTR", "DMQ", "FTQ"):
                    out["cargo"].setdefault("_gid_volume", 0.0)
                    out["cargo"]["_gid_volume"] += _volume_m3(val, unit) or 0
            elif s.tag == "DIM":
                unit = s.el(2, 1)
                l, w, h = (_num(s.el(2, i)) for i in (2, 3, 4))
                if l and w and h:
                    f = {"CMT": 1, "MTR": 100, "MMT": 0.1}.get(unit, 1)
                    dims.append({"length_cm": l * f, "width_cm": w * f, "height_cm": h * f, "count": int(n or 1)})
            elif s.tag == "DGS":
                # DGS: 1 regulation, 2 C205 hazard class, 3 C234 UN number, 5 packing danger level (1/2/3 = I/II/III)
                dg = {"un_number": s.el(3, 1), "class": s.el(2, 1),
                      "packing_group": {"1": "I", "2": "II", "3": "III"}.get(s.el(5), s.el(5) or None),
                      "regulation": s.el(1) or None}
            elif s.tag == "FTX" and s.el(1) == "AAD" and dg is not None:  # DG proper shipping name
                dg["proper_shipping_name"] = " ".join(p for p in s.comps(4) if p)
            elif s.tag == "COM" and dg is not None and s.el(1, 2) == "TE":
                dg["emergency_phone"] = s.el(1, 1)
    for g in msg.groups("EQD"):
        eqd = g[0]
        if eqd.el(1) != "CN":
            continue
        ctype = codes.ISO_CONTAINERS.get(eqd.el(3, 1).upper())
        if not ctype:
            errors.append(f"Container size/type {eqd.el(3, 1)!r} is not one we book (use 22G1, 42G1, 45G1, L5G1, 22R1 or 45R1).")
            continue
        count = 1
        for s in g[1:]:
            if s.tag == "EQN":
                count = int(_num(s.el(1, 1)) or 1)
        containers[ctype] = containers.get(ctype, 0) + count
    cargo = out["cargo"]
    gid_w, gid_v = cargo.pop("_gid_weight", None), cargo.pop("_gid_volume", None)
    gid_pk = cargo.pop("packages_from_gid", None)
    cargo["weight_kg"] = cargo.get("weight_kg") or (round(gid_w, 3) if gid_w else None)
    cargo["volume_m3"] = cargo.get("volume_m3") or (round(gid_v, 3) if gid_v else None)
    cargo["packages"] = cargo.get("packages") or gid_pk
    cargo["package_type"] = pkg_type
    cargo["description"] = "; ".join(d for d in descriptions if d)[:500] or None
    if dims:
        cargo["dims"] = dims
    if containers:
        cargo["load_type"] = "FCL"
        cargo["containers"] = [{"type": t, "count": c} for t, c in containers.items()]
    else:
        cargo["load_type"] = "loose"
    if dg:
        cargo["dangerous_goods"] = True
        cargo["dg"] = dg
    out["instructions"] = " ".join(t for t in texts if t)[:2000] or None
    # What a booking needs
    if out["function"] not in (codes.FN_CANCEL,):
        if not out["origin"] or not (out["origin"].get("unlocode") or out["origin"].get("name")):
            errors.append("Place of departure / loading (LOC+5 or LOC+9) is missing.")
        if not out["destination"] or not (out["destination"].get("unlocode") or out["destination"].get("name")):
            errors.append("Place of delivery / discharge (LOC+7 or LOC+11) is missing.")
        for role in ("shipper", "consignee"):
            p = out["parties"].get(role)
            if not p or not p.get("name"):
                errors.append(f"The {role} (NAD+{'CZ' if role == 'shipper' else 'CN'}) is missing.")
            else:
                a = p["address"]
                if not (a.get("street") and a.get("city") and a.get("country")):
                    errors.append(f"The {role}'s street, city and country (NAD elements 5, 6 and 9) are required.")
        if cargo["load_type"] != "FCL" and not cargo.get("weight_kg"):
            errors.append("Total gross weight (CNT+7 or MEA+AAE+AAB) is missing.")
    return out


def _apply_loc(out: dict, s: Segment):
    q = s.el(1)
    loc = _loc(s)
    if q in ("5", "9", "88", "198") and not out["origin"]:
        out["origin"] = loc
    elif q in ("5",):
        out["origin"] = loc
    elif q in ("7", "11", "8", "83") and not out["destination"]:
        out["destination"] = loc
    elif q == "7":
        out["destination"] = loc
    elif q == "1" and not out.get("incoterm_place"):
        out["incoterm_place"] = loc.get("name") or loc.get("unlocode")
