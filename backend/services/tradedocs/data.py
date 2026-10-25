"""The data every trade document is printed from: one normalised context per shipment.

build_context(shipment) reads the shipment, its company, carriers, customs declaration and the incoming documents the
extraction agent has read, and returns a plain dict (JSON-safe, stored as the document version's snapshot):

    shipment   {id, reference, mode, direction, incoterm, incoterm_place, status, service, customer_ref, po_numbers,
                booking_no, etd, eta, atd, ata, forwarder_role, liability_regime, customs_clearance}
    issuer     the forwarder (Huolitsija) as carrier of the house document: {name, address{...}, business_id, vat_id, eori,
               email, phone}
    shipper / consignee / notify / customer  party {name, address{street, postal_code, city, country}, contact_name,
               email, phone, vat_id, eori, business_id}  (notify may be None = "same as consignee")
    origin / destination  {name, unlocode, country}  (door ends when the shipment has door legs)
    place_of_receipt, port_of_loading, port_of_discharge, place_of_delivery   {name, unlocode, country}
    main_leg   {seq, mode, carrier, carrier_code, vessel, imo, voyage, flight, etd, eta, from, to}
    legs       [{seq, mode, role, carrier, carrier_id, carrier_code, carrier_email, from, to, etd, eta, atd, ata, vessel,
                 voyage, flight, truck_plate, transport_doc_no}]
    road_legs_international  [legs]  road legs that cross a border (CMR)
    delivery_carrier         {name, email, code} of the last leg (delivery order)
    goods      [{seq, description, hs_code, origin_country, quantity, unit, unit_price, amount, currency, packages,
                 package_type, net_kg, gross_kg, volume_m3, marks}]   (source: customs declaration > invoice extraction > booking)
    goods_source  "customs_declaration" | "commercial_invoice" | "booking"
    totals     {packages, package_type, gross_kg, net_kg, volume_m3, amount, currency, chargeable_kg}
    containers [{number, type, seal, gross_kg, packages}]
    dangerous_goods  [{un_number, proper_shipping_name, class, subsidiary, packing_group, packages, gross_kg, net_kg,
                       ems, limited_quantity, marine_pollutant, flash_point}]   ([] = none)
    freight    {terms: prepaid|collect, payable_at, currency, total_cents, charges[{code, description, amount_cents}]}
    insurance  {...}
    dates      {issue, shipped_on_board, taking_over}
    refs       {shipment, customer, po, booking, awb_master, mbl, container_numbers}
"""
from __future__ import annotations

import csv
import functools
import os
import re

from core import config
from core.db import db
from core.util import NO_ID, now_iso

# Incoterms where the seller pays the main carriage: freight prepaid at origin. The rest: collect at destination.
PREPAID_TERMS = {"CPT", "CIP", "CFR", "CIF", "DAP", "DPU", "DDP"}

PACKAGE_CODES = {  # UN/ECE Recommendation 21 package type codes (used in DCSA, e-CMR and IATA messages)
    "cartons": "CT", "carton": "CT", "pallets": "PX", "pallet": "PX", "bags": "BG", "bag": "BG", "crates": "CR",
    "crate": "CR", "boxes": "BX", "box": "BX", "bundles": "BE", "bundle": "BE", "coils": "CL", "coil": "CL",
    "drums": "DR", "drum": "DR", "rolls": "RO", "pieces": "PC", "pcs": "PC", "cases": "CS", "case": "CS",
    "packages": "PK", "sacks": "SA", "bales": "BL", "reels": "RL", "cylinders": "CY", "ibc": "WW",
}

DG_CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "dg", "hmt_172_101.csv")


def package_code(kind: str | None) -> str:
    return PACKAGE_CODES.get((kind or "").strip().lower(), "PK")


@functools.lru_cache(maxsize=1)
def dg_table() -> dict[str, list[dict]]:
    """UN number -> entries of the US DOT Hazardous Materials Table (49 CFR 172.101, public domain), which carries the
    UN Model Regulations proper shipping names, classes and packing groups used by IMDG / IATA DGR / ADR."""
    out: dict[str, list[dict]] = {}
    if not os.path.exists(DG_CSV):
        return out
    with open(DG_CSV, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            un = (row.get("un_number") or "").strip().upper()
            if re.fullmatch(r"UN\d{4}", un):
                out.setdefault(un, []).append(row)
    return out


def dg_lookup(un: str | None, cls: str | None = None) -> dict | None:
    un = (un or "").strip().upper().replace(" ", "")
    if re.fullmatch(r"\d{4}", un):
        un = "UN" + un
    rows = dg_table().get(un) or []
    if cls:
        same = [r for r in rows if (r.get("hazard_class") or "").split()[0] == str(cls)]
        rows = same or rows
    return rows[0] if rows else None


def _addr(a: dict | None) -> dict:
    a = a or {}
    return {"street": a.get("street") or a.get("line1") or "", "postal_code": a.get("postal_code") or a.get("zip") or "",
            "city": a.get("city") or "", "country": (a.get("country") or "").upper()}


def _party(p: dict | None, fallback_country: str | None = None) -> dict | None:
    if not p or not (p.get("name") or "").strip():
        return None
    addr = _addr(p.get("address"))
    if not addr["country"] and fallback_country:
        addr["country"] = fallback_country
    return {"name": p.get("name").strip(), "address": addr, "contact_name": p.get("contact_name"), "email": p.get("email"),
            "phone": p.get("phone"), "vat_id": p.get("vat_id"), "eori": p.get("eori"), "business_id": p.get("business_id"),
            **({"ebl_platform": p["ebl_platform"], "ebl_party_code": p.get("ebl_party_code")} if p.get("ebl_platform") else {})}


def company_party(c: dict | None) -> dict | None:
    if not c:
        return None
    return {"name": c.get("name"), "address": _addr(c.get("address") or {"country": c.get("country")}),
            "contact_name": c.get("contact_name"), "email": c.get("billing_email") or c.get("email"), "phone": c.get("phone"),
            "vat_id": c.get("vat_id"), "eori": c.get("eori"), "business_id": c.get("business_id")}


def issuer() -> dict:
    """The forwarder as issuer / contracting carrier of the house documents (TRADEDOCS_ISSUER_* overrides COMPANY_*)."""
    e = config.env
    address = e("TRADEDOCS_ISSUER_ADDRESS", config.COMPANY_ADDRESS) or ""
    parts = [x.strip() for x in address.split(",") if x.strip()]
    city = parts[-2] if len(parts) >= 2 else (parts[0] if parts else "Helsinki")
    m = re.match(r"(\d{5})\s+(.*)", city)
    return {"name": e("TRADEDOCS_ISSUER_NAME", config.COMPANY_NAME), "address": {
        "street": parts[0] if len(parts) > 2 else "", "postal_code": m.group(1) if m else "", "city": m.group(2) if m else city,
        "country": "FI"}, "business_id": config.COMPANY_BUSINESS_ID or None, "vat_id": config.COMPANY_VAT_ID or None,
        "eori": config.COMPANY_EORI or None, "email": e("TRADEDOCS_ISSUER_EMAIL", config.COMPANY_EMAIL),
        "phone": config.COMPANY_PHONE or None, "scac": e("TRADEDOCS_ISSUER_SCAC", "") or None,
        "iata_agent_code": e("TRADEDOCS_IATA_AGENT_CODE", "") or None}


def _place(loc: dict | None) -> dict:
    loc = loc or {}
    return {"name": loc.get("name") or (loc.get("address") or {}).get("city") or "", "unlocode": loc.get("unlocode") or loc.get("code"),
            "country": (loc.get("country") or (loc.get("address") or {}).get("country") or "").upper(),
            "address": _addr(loc.get("address")) if loc.get("address") else None,
            "iata": loc.get("iata") or (loc.get("code") if re.fullmatch(r"[A-Z]{3}", str(loc.get("code") or "")) else None)}


async def _carrier(leg: dict) -> dict:
    c = None
    if leg.get("carrier_id"):
        c = await db.carriers.find_one({"id": leg["carrier_id"]}, NO_ID)
    if not c and leg.get("carrier"):
        c = await db.carriers.find_one({"name": leg["carrier"]}, NO_ID)
    c = c or {}
    email = c.get("ops_email") or c.get("booking_email") or c.get("contact_email") or c.get("email") or (c.get("contact") or {}).get("email")
    return {"name": leg.get("carrier") or c.get("name"), "code": c.get("code") or c.get("scac") or c.get("iata_code"),
            "scac": c.get("scac"), "iata_prefix": c.get("awb_prefix") or c.get("iata_prefix"), "email": email,
            "id": c.get("id") or leg.get("carrier_id")}


def _num(x):
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def _declaration(s: dict) -> dict | None:
    decl = None
    if s.get("customs_declaration_ids"):
        decl = await db.customs_declarations.find_one({"id": {"$in": s["customs_declaration_ids"]}}, NO_ID,
                                                       sort=[("created_at", -1)])
    return decl or await db.customs_declarations.find_one({"shipment_id": s["id"]}, NO_ID, sort=[("created_at", -1)])


async def _goods(s: dict, *, prefer_invoice: bool = False, source_invoice: dict | None = None) -> tuple[list[dict], str]:
    cargo = s.get("cargo") or {}
    decl = None if prefer_invoice else await _declaration(s)
    lines: list[dict] = []
    source = "booking"
    if decl and decl.get("items"):
        source = "customs_declaration"
        for i, it in enumerate(decl["items"], start=1):
            lines.append({"seq": i, "description": it.get("description"), "hs_code": it.get("cn_code") or it.get("hs_code") or it.get("hs_hint"),
                          "origin_country": it.get("origin_country"), "quantity": _num(it.get("quantity")), "unit": it.get("unit"),
                          "unit_price": _num(it.get("unit_price")), "amount": _num(it.get("invoice_amount")),
                          "currency": it.get("currency") or "EUR", "packages": _num(it.get("packages")),
                          "package_type": it.get("package_type") or cargo.get("package_type"),
                          "net_kg": _num(it.get("net_kg")), "gross_kg": _num(it.get("gross_kg")), "volume_m3": None, "marks": None})
    if not lines:
        inv = source_invoice or await db.documents.find_one({"shipment_id": s["id"], "type": "commercial_invoice",
                                           "extraction.status": "done" if prefer_invoice else {"$in": ["done", "needs_review"]}}, NO_ID, sort=[("uploaded_at", -1)])
        items = ((inv or {}).get("extraction") or {}).get("line_items") or []
        fields = ((inv or {}).get("extraction") or {}).get("fields") or {}
        cur = ((fields.get("currency") or {}).get("value") if isinstance(fields.get("currency"), dict) else None) or "EUR"
        origin = (fields.get("country_of_origin") or {}).get("value") if isinstance(fields.get("country_of_origin"), dict) else None
        for i, li in enumerate(items, start=1):
            v = {k: (x.get("value") if isinstance(x, dict) else x) for k, x in li.items()}
            lines.append({"seq": i, "description": v.get("description"), "hs_code": v.get("hs_code"),
                          "origin_country": v.get("country_of_origin") or origin, "quantity": _num(v.get("quantity")),
                          "unit": v.get("unit"), "unit_price": _num(v.get("unit_price")), "amount": _num(v.get("amount")),
                          "currency": cur, "packages": _num(v.get("packages")), "package_type": v.get("package_type"),
                          "net_kg": _num(v.get("net_weight_kg")), "gross_kg": _num(v.get("gross_weight_kg")), "volume_m3": None,
                          "marks": None})
        if lines:
            source = "commercial_invoice"
    if not lines:
        value = cargo.get("value_cents")
        lines.append({"seq": 1, "description": cargo.get("description"), "hs_code": cargo.get("hs_hint"),
                      "origin_country": cargo.get("origin_country"),
                      "quantity": _num(cargo.get("packages")), "unit": cargo.get("package_type"), "unit_price": None,
                      "amount": (value / 100) if value else None, "currency": cargo.get("currency") or "EUR",
                      "packages": _num(cargo.get("packages")), "package_type": cargo.get("package_type"),
                      "net_kg": _num(cargo.get("net_weight_kg")), "gross_kg": _num(cargo.get("weight_kg")),
                      "volume_m3": _num(cargo.get("volume_m3")), "marks": cargo.get("marks")})
    # Consignment totals are not per-line measurements. A single line may use the whole
    # consignment weight; never allocate weights across goods by an invented quantity share.
    tot_pk, tot_kg = _num(cargo.get("packages")), _num(cargo.get("weight_kg"))
    if len(lines) == 1:
        if lines[0]["gross_kg"] is None and tot_kg is not None:
            lines[0]["gross_kg"] = tot_kg
        if lines[0]["packages"] is None and tot_pk is not None:
            lines[0]["packages"] = tot_pk
            lines[0]["package_type"] = lines[0]["package_type"] or cargo.get("package_type")
    return lines, source


def _chargeable_air(gross: float | None, volume: float | None) -> float | None:
    """IATA: chargeable weight = max(actual, volume weight at 6,000 cm3/kg), rounded up to the next 0.5 kg."""
    import math
    if gross is None and volume is None:
        return None
    vol_kg = (volume or 0) * 1_000_000 / 6000
    w = max(gross or 0, vol_kg)
    return math.ceil(w * 2) / 2


async def build_context(s: dict, *, kind: str | None = None) -> dict:
    company = await db.companies.find_one({"id": s.get("company_id")}, NO_ID) or {}
    cargo = s.get("cargo") or {}
    parties = s.get("parties") or {}
    refs = s.get("references") or {}
    customer = company_party(company)
    direction = s.get("direction") or "import"
    shipper = _party(parties.get("shipper"), (s.get("origin") or {}).get("country")) or (customer if direction == "export" else None)
    consignee = _party(parties.get("consignee"), (s.get("destination") or {}).get("country")) or (customer if direction != "export" else None)
    notify = _party(parties.get("notify"))
    legs_out = []
    for lg in s.get("legs") or []:
        c = await _carrier(lg)
        vessel = lg.get("vessel") or {}
        legs_out.append({"seq": lg.get("seq"), "mode": lg.get("mode"), "role": lg.get("role"), "service": lg.get("service"),
                         "carrier": c["name"], "carrier_id": c["id"], "carrier_code": c["code"], "carrier_email": c["email"],
                         "carrier_scac": c["scac"], "carrier_iata_prefix": c["iata_prefix"],
                         "from": _place(lg.get("from")), "to": _place(lg.get("to")), "etd": lg.get("etd"), "eta": lg.get("eta"),
                         "atd": lg.get("atd"), "ata": lg.get("ata"),
                         "vessel": vessel.get("name") if isinstance(vessel, dict) else vessel,
                         "imo": vessel.get("imo") if isinstance(vessel, dict) else None,
                         "voyage": lg.get("voyage"), "flight": lg.get("flight"), "truck_plate": lg.get("truck_plate"),
                         "transport_doc_no": lg.get("transport_doc_no"), "container_numbers": lg.get("container_numbers") or []})
    mode = s.get("mode") or (legs_out[0]["mode"] if legs_out else "sea")
    mains = [lg for lg in legs_out if lg["mode"] == mode and lg["role"] in ("main", "feeder", "onward")] or \
        [lg for lg in legs_out if lg["mode"] == mode] or legs_out
    main = mains[0] if mains else {}
    last_main = mains[-1] if mains else {}
    pol, pod = (main.get("from") or _place(s.get("origin"))), (last_main.get("to") or _place(s.get("destination")))
    road_intl = [lg for lg in legs_out if lg["mode"] == "road" and lg["from"]["country"] and lg["to"]["country"]
                 and lg["from"]["country"] != lg["to"]["country"]]
    if mode == "road" and not road_intl and legs_out:
        o, d = _place(s.get("origin")), _place(s.get("destination"))
        if o["country"] and d["country"] and o["country"] != d["country"]:
            road_intl = [lg for lg in legs_out if lg["mode"] == "road"][:1]
    source_document_id = None
    invoice = None
    if kind == "commercial_invoice":
        invoice = await db.documents.find_one({"shipment_id": s["id"], "type": "commercial_invoice",
                                              "extraction.status": "done"}, NO_ID, sort=[("uploaded_at", -1)])
        if invoice:
            source_document_id = invoice["id"]
            fields = invoice["extraction"].get("fields") or {}
            def field(name):
                v = fields.get(name)
                return v.get("value") if isinstance(v, dict) else v
            def source_party(prefix):
                return {"name": field(prefix + "_name"), "address": {"street": field(prefix + "_address") or "",
                        "country": field(prefix + "_country") or "", "city": "", "postal_code": ""},
                        "vat_id": field(prefix + "_vat_id"), "eori": field(prefix + "_eori")}
            # Invoice parties and invoice lines must come from the same source. A shipment's
            # booking party can differ (e.g. consolidator); never silently replace the seller.
            shipper, consignee = source_party("seller"), source_party("buyer")
    invoice_reference = refs.get("invoice_number")
    if kind == "eur1_application" and not invoice_reference:
        source = await db.documents.find_one({"shipment_id": s["id"], "type": "commercial_invoice",
                                             "extraction.status": "done"}, NO_ID, sort=[("uploaded_at", -1)])
        value = (((source or {}).get("extraction") or {}).get("fields") or {}).get("invoice_number")
        invoice_reference = value.get("value") if isinstance(value, dict) else value
    goods, goods_source = await _goods(s, prefer_invoice=kind == "commercial_invoice", source_invoice=invoice)
    gross = _num(cargo.get("weight_kg")) or sum(g["gross_kg"] or 0 for g in goods) or None
    volume = _num(cargo.get("volume_m3"))
    amount = sum(g["amount"] for g in goods) if goods and all(g["amount"] is not None for g in goods) else None
    currency = next((g["currency"] for g in goods if g.get("currency")), "EUR")
    conts = []
    types = []
    for c in cargo.get("containers") or []:
        types += [str(c.get("type") or "40HC")] * int(c.get("count") or 1)
    for i, num in enumerate(refs.get("container_numbers") or []):
        conts.append({"number": num, "type": types[i] if i < len(types) else (types[0] if types else "40HC"),
                      "seal": (refs.get("seals") or {}).get(num) if isinstance(refs.get("seals"), dict) else None,
                      "gross_kg": gross if len(refs["container_numbers"]) == 1 else None,
                      "packages": None})
    dg = []
    if cargo.get("dangerous_goods"):
        entries = cargo.get("dg_items") or ([cargo.get("dg")] if cargo.get("dg") else [])
        for e in entries:
            row = dg_lookup(e.get("un"), e.get("class")) or {}
            dg.append({"un_number": (e.get("un") or row.get("un_number") or "").upper(),
                       "proper_shipping_name": e.get("proper_shipping_name") or row.get("proper_shipping_name"),
                       "class": str(e.get("class") or ((row.get("hazard_class") or "").split() or [""])[0]),
                       "subsidiary": e.get("subsidiary") or None, "packing_group": e.get("packing_group") or row.get("packing_group") or None,
                       "packages": e.get("packages") or cargo.get("packages"), "package_type": e.get("package_type") or cargo.get("package_type"),
                       "gross_kg": e.get("gross_kg") or cargo.get("weight_kg"), "net_kg": e.get("net_kg"),
                       "ems": e.get("ems"), "limited_quantity": bool(e.get("limited_quantity")),
                       "marine_pollutant": bool(e.get("marine_pollutant")), "flash_point": e.get("flash_point"),
                       "label_codes": row.get("label_codes"), "special_provisions": row.get("special_provisions"),
                       "source": "49 CFR 172.101" if row else "booking"})
    incoterm = (s.get("incoterm") or "").upper() or None
    terms = "prepaid" if incoterm in PREPAID_TERMS else "collect"
    price = s.get("price") or {}
    last = legs_out[-1] if legs_out else {}
    shipped = main.get("atd")
    taking_over = legs_out[0].get("atd") if legs_out else None
    return {
        "shipment": {"id": s["id"], "reference": s.get("reference"), "mode": mode, "direction": direction, "incoterm": incoterm,
                     "incoterm_place": s.get("incoterm_place"), "status": s.get("status"), "service": s.get("service"),
                     "customer_ref": refs.get("customer_ref"), "po_numbers": refs.get("po_numbers") or [],
                     "booking_no": refs.get("booking_no"), "etd": s.get("etd"), "eta": s.get("eta"), "atd": s.get("atd"),
                     "ata": s.get("ata"), "forwarder_role": s.get("forwarder_role"), "liability_regime": s.get("liability_regime"),
                     "customs_clearance": s.get("customs_clearance"), "demo": bool(s.get("demo")), "company_id": s.get("company_id")},
        "issuer": issuer(), "customer": customer, "shipper": shipper, "consignee": consignee, "notify": notify,
        "origin": _place(s.get("origin")), "destination": _place(s.get("destination")),
        "place_of_receipt": legs_out[0]["from"] if legs_out and legs_out[0]["mode"] != mode else pol,
        "port_of_loading": pol, "port_of_discharge": pod,
        "place_of_delivery": last.get("to") if last and last.get("seq") != last_main.get("seq") else pod,
        "main_leg": {k: main.get(k) for k in ("seq", "mode", "carrier", "carrier_code", "carrier_scac", "carrier_iata_prefix",
                                              "vessel", "imo", "voyage", "flight", "etd", "eta", "atd", "ata", "from", "to")},
        "legs": legs_out, "road_legs_international": road_intl,
        "delivery_carrier": {"name": last.get("carrier"), "email": last.get("carrier_email"), "code": last.get("carrier_code"),
                             "truck_plate": last.get("truck_plate")} if last else None,
        "goods": goods, "goods_source": goods_source, "source_document_id": source_document_id,
        "totals": {"packages": _num(cargo.get("packages")) or sum(g["packages"] or 0 for g in goods) or None,
                   "package_type": cargo.get("package_type"), "gross_kg": gross,
                   "net_kg": sum(g["net_kg"] or 0 for g in goods) or None, "volume_m3": volume, "amount": amount,
                   "currency": currency, "chargeable_kg": _chargeable_air(gross, volume) if mode == "air" else None,
                   "ldm": _num(cargo.get("ldm"))},
        "containers": conts, "dangerous_goods": dg,
        "freight": {"terms": terms, "payable_at": (pol if terms == "prepaid" else pod).get("name"),
                    "currency": price.get("currency") or "EUR", "total_cents": price.get("total_cents"),
                    "charges": price.get("lines") or []},
        "insurance": s.get("insurance") or {},
        "dates": {"issue": now_iso(), "shipped_on_board": shipped, "taking_over": taking_over},
        "refs": {"shipment": s.get("reference"), "customer": refs.get("customer_ref"), "po": refs.get("po_numbers") or [],
                 "booking": refs.get("booking_no"), "awb_master": refs.get("awb_no"), "mbl": refs.get("mbl_no"),
                 "invoice": invoice_reference,
                 "container_numbers": refs.get("container_numbers") or []},
        "cargo_description": cargo.get("description"),
        "customs": await _customs(s),
        **({"delivery_release": s.get("delivery_release")} if kind == "delivery_order" else {}),
        "negotiable": bool((s.get("references") or {}).get("bl_to_order") or (s.get("documents_prefs") or {}).get("negotiable_bl")),
    }


async def _customs(s: dict) -> dict:
    decl = await _declaration(s)
    released = next((m.get("at") for m in s.get("milestones") or [] if m.get("code") == "customs_released" and m.get("at")), None)
    if not decl:
        return {"required": bool(s.get("customs_clearance")), "status": None, "mrn": None, "lrn": None, "released_at": released}
    return {"required": True, "status": decl.get("status"), "mrn": decl.get("mrn"), "lrn": decl.get("lrn"),
            "released_at": released or decl.get("released_at"), "procedure": decl.get("procedure") or decl.get("type")}


def set_path(ctx: dict, path: str, value):
    """Apply one amendment change: dotted path, list indexes allowed (goods.0.description)."""
    parts = path.split(".")
    cur = ctx
    for i, p in enumerate(parts[:-1]):
        nxt = parts[i + 1]
        key = int(p) if isinstance(cur, list) else p
        if isinstance(cur, list):
            cur = cur[key]
        else:
            if cur.get(key) is None:
                cur[key] = [] if nxt.isdigit() else {}
            cur = cur[key]
    last = parts[-1]
    if isinstance(cur, list):
        idx = int(last)
        old = cur[idx] if idx < len(cur) else None
        if idx < len(cur):
            cur[idx] = value
        else:
            cur.append(value)
        return old
    old = cur.get(last)
    cur[last] = value
    return old


def get_path(ctx: dict, path: str):
    cur = ctx
    for p in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(p)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur
