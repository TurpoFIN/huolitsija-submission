"""Quote pricing engine (V2).

    options = await price_request(request, company=company)          # contract signature
    result  = await quote_pricing(request, company=company)          # options + warnings + not_quoted + normalised request

How a price is built
--------------------
1. The request is normalised: locations resolved (V1 services.geo -> `locations` collection -> V2 network table),
   direction derived (import / export / domestic / cross_trade), cargo measured (weight, volume, loading metres,
   containers), Incoterm validated (domain-spec RULE I-1, I-2, legacy terms rejected).
2. Rate cards (collection `rate_cards`, buy prices) form a graph between UN/LOCODE nodes. A route is
   [pre-carriage] + main card + [connection card at the same hub, e.g. feeder or rail] + [on-carriage].
   Pre/on-carriage uses the country's domestic tariffs (region cards) when the place is outside the card's
   included collection/delivery radius.
3. Each card is priced on its basis with the chargeable quantity rules:
     air 1:6000 (167 kg/m³) · road max(kg, 333 kg/m³, 1 850 kg/ldm) · LCL W/M 1 m³ = 1 000 kg · FCL per container.
   Weight-break tiers, distance bands, minimum charges and surcharges (BAF, EU ETS, FuelEU, THC, ISPS, B/L, VGM,
   air fuel/security, road fuel %) apply. Sell price = cost x (1 + markup) from core.settings or the company.
4. Forwarder fees (documentation, customs clearance, ICS2), insurance (RULE X-1, 110 % of CIF value), DG surcharge
   (RULE D-5) and estimated duty / import VAT pass-through lines are added. Each line carries a VAT code
   (RULE V-1) and a payer derived from the Incoterm (RULE Q-3); lines the other party pays are listed under
   `not_included`.
5. Options get ETD/ETA from departure weekdays (or a live carrier schedule when that integration is live),
   CO2e from GLEC-style default intensities x distance x weight, liability regime (RULE X-3), and are ranked:
   cheapest, fastest, lowest CO2 and best value (recommended).
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from datetime import date, datetime, timedelta, timezone

from core.db import db
from core.settings import get_settings
from core.util import NO_ID, now
from services import agent_network as agents, allocations, scorecards
from services import carriers as net
from services import contract_tariffs, margin_rules, customs_border

HOME_COUNTRY = "FI"  # Huolitsija handles Finnish ends itself; overseas ends may use an agent (s3)
log = logging.getLogger("pricing")


class QuoteInputError(ValueError):
    """Request cannot be priced; message is a plain sentence for the user."""


# ------------------------------------------------------------------------------------------------
# Reference data
# ------------------------------------------------------------------------------------------------
INCOTERMS_2020 = {
    "EXW": "Ex Works", "FCA": "Free Carrier", "CPT": "Carriage Paid To", "CIP": "Carriage and Insurance Paid To",
    "DAP": "Delivered at Place", "DPU": "Delivered at Place Unloaded", "DDP": "Delivered Duty Paid",
    "FAS": "Free Alongside Ship", "FOB": "Free On Board", "CFR": "Cost and Freight", "CIF": "Cost, Insurance and Freight",
}
SEA_ONLY_INCOTERMS = {"FAS", "FOB", "CFR", "CIF"}
LEGACY_INCOTERMS = {"DAT": "DPU", "DDU": "DAP", "DAF": "DAP", "DES": "DAP", "DEQ": "DPU"}
# Stages the SELLER pays under each Incoterm (domain-spec section 1, RULE Q-3). Everything else is the buyer's.
SELLER_STAGES = {
    "EXW": set(),
    "FCA": {"export_customs"},
    "FAS": {"export_customs", "origin"},
    "FOB": {"export_customs", "origin"},
    "CFR": {"export_customs", "origin", "main"},
    "CPT": {"export_customs", "origin", "main"},
    "CIF": {"export_customs", "origin", "main", "insurance"},
    "CIP": {"export_customs", "origin", "main", "insurance"},
    "DAP": {"export_customs", "origin", "main", "destination"},
    "DPU": {"export_customs", "origin", "main", "destination"},
    "DDP": {"export_customs", "origin", "main", "destination", "import_customs", "pass_through"},
}

CONTAINERS = {
    "20GP": {"size": "20", "teu": 1, "cbm": 28.0, "kg": 21000, "label": "20' GP"},
    "40GP": {"size": "40", "teu": 2, "cbm": 56.0, "kg": 26500, "label": "40' GP"},
    "40HC": {"size": "40", "teu": 2, "cbm": 66.0, "kg": 26500, "label": "40' HC"},
    "45HC": {"size": "45", "teu": 2.25, "cbm": 74.0, "kg": 27500, "label": "45' HC"},
    "20RF": {"size": "20", "teu": 1, "cbm": 25.0, "kg": 21000, "label": "20' reefer"},
    "40RF": {"size": "40", "teu": 2, "cbm": 58.0, "kg": 26000, "label": "40' reefer"},
}
PALLET_LDM = {"pallet_eur": 0.4, "pallet_fin": 0.5}  # 1.2 x 0.8 m and 1.2 x 1.0 m on a 2.4 m wide trailer
PACKAGE_LABEL = {"pallet_eur": "EUR pallet", "pallet_fin": "FIN pallet", "carton": "carton", "crate": "crate", "other": "package"}
TRAILER_LDM = 13.6
TRAILER_KG = 24000
AIR_KG_PER_M3 = 1_000_000 / 6000  # 166.67 kg per m³ (IATA 1:6000)
ROAD_KG_PER_M3 = 333.0
ROAD_KG_PER_LDM = 1850.0
LCL_KG_PER_WM = 1000.0
AVG_TONNES_PER_TEU = 10.0  # CO2 allocation when FCL weight is not given
MAX_HAUL_KM = {"loose": 800, "fcl": 450}

MODE_OF_SERVICE = {"FCL": "sea", "LCL": "sea", "RORO": "sea", "AIR": "air", "ROAD_LTL": "road", "ROAD_FTL": "road",
                   "HAULAGE": "road", "RAIL_FCL": "rail", "RAIL_WAGON": "rail", "COURIER": "air"}
SERVICE_NAME = {"FCL": "FCL", "LCL": "LCL", "RORO": "Ro-ro trailer", "AIR": "Air", "ROAD_LTL": "Road groupage",
                "ROAD_FTL": "Full truck", "HAULAGE": "Haulage", "RAIL_FCL": "Rail container", "RAIL_WAGON": "Rail wagon"}

VAT_RATES = {"FI_25_5": 25.5, "FI_13_5": 13.5, "FI_14": 13.5, "FI_10": 10.0, "ZERO_EXPORT": 0.0, "ZERO_INTL_TRANSPORT": 0.0,
             "EU_ICS_RC": 0.0, "OUT_OF_SCOPE_DISBURSEMENT": 0.0, "OUT_OF_SCOPE_FOREIGN": 0.0}
VAT_CODES = set(VAT_RATES)
# RULE V-1. Domestic Finnish legs are 25.5 % per domain-spec section 4.2 / 9 (verify with Vero);
# customs brokerage tied to imported/exported goods is zero-rated (ALV 71-72 §, verify). Both are env switches.
DOMESTIC_LEG_VAT_CODE = os.environ.get("QUOTE_DOMESTIC_LEG_VAT_CODE", "FI_25_5")
CUSTOMS_FEE_VAT_CODE = os.environ.get("QUOTE_CUSTOMS_FEE_VAT_CODE", "ZERO_INTL_TRANSPORT")
ICS2_FEE_EUR = float(os.environ.get("QUOTE_ICS2_FEE_EUR", "25"))
CUSTOMS_INCLUDED_ITEMS = int(os.environ.get("QUOTE_CUSTOMS_INCLUDED_ITEMS", "5"))
LOW_VALUE_DUTY_FROM = date(2026, 7, 1)  # RULE L-2: EUR 3 per item category on consignments <= EUR 150

STAGE_OF_CODE = {
    "OTHC": "origin", "VGM": "origin", "DOC": "origin", "CFS_O": "origin", "AWB": "origin", "PICKUP": "origin",
    "BAF": "main", "ETS": "main", "FUELEU": "main", "LSS": "main", "CAF": "main", "PSS": "main", "GRI": "main",
    "FSC": "main", "SSC": "main", "ROAD_FUEL": "main", "FERRY": "main", "ISPS": "main", "DG": "main", "HUB_THC": "main",
    "TOLL": "main", "CASPIAN": "main", "TRAILER": "main", "BORDER": "main", "WAR_RISK": "main", "WRS": "main",
    "PSS": "main", "CAF": "main", "COURIER": "main",
    "DTHC": "destination", "DO": "destination", "CFS_D": "destination", "HANDLING_D": "destination", "DELIVERY": "destination",
}

LINE_TEXT = {
    "OCEAN_FREIGHT": ("Ocean freight", "Merirahti"),
    "FEEDER_FREIGHT": ("Feeder freight", "Syöttöliikenteen rahti"),
    "AIR_FREIGHT": ("Air freight", "Lentorahti"),
    "ROAD_FREIGHT": ("Road freight", "Maantiekuljetus"),
    "RORO_FREIGHT": ("Ro-ro freight, unaccompanied trailer", "Ro-ro-rahti, perävaunu ilman kuljettajaa"),
    "RAIL_FREIGHT": ("Rail freight", "Rautatierahti"),
    "PICKUP": ("Pickup / pre-carriage", "Nouto / esikuljetus"),
    "DELIVERY": ("Delivery / on-carriage", "Jakelu / jatkokuljetus"),
    "DELIVERY_FI": ("Delivery (Finland domestic)", "Kotimaan jakelu"),
    "PICKUP_FI": ("Pickup (Finland domestic)", "Kotimaan nouto"),
    "BAF": ("Bunker adjustment (BAF)", "Polttoainelisä (BAF)"),
    "ETS": ("EU ETS surcharge", "EU:n päästökauppalisä (ETS)"),
    "FUELEU": ("FuelEU Maritime surcharge", "FuelEU Maritime -lisä"),
    "LSS": ("Low-sulphur surcharge", "Rikkilisä"),
    "OTHC": ("Origin terminal handling (THC)", "Lähtöterminaalimaksu (THC)"),
    "DTHC": ("Destination terminal handling (THC)", "Määräterminaalimaksu (THC)"),
    "HUB_THC": ("Transshipment hub handling", "Uudelleenlastaus välisatamassa"),
    "ISPS": ("ISPS port security", "ISPS-turvamaksu"),
    "DOC": ("Documentation / bill of lading fee", "Asiakirjamaksu / konossementti"),
    "VGM": ("VGM verified gross mass (SOLAS)", "VGM-punnitus (SOLAS)"),
    "DO": ("Delivery order fee", "Luovutusmääräys (D/O)"),
    "CFS_O": ("Origin CFS / consolidation", "Lähtöterminaalin konsolidointi (CFS)"),
    "CFS_D": ("Destination CFS / devanning", "Määräterminaalin purku (CFS)"),
    "AWB": ("Air waybill fee", "Lentorahtikirjamaksu"),
    "FSC": ("Air fuel surcharge", "Lentopolttoainelisä"),
    "SSC": ("Air security / screening", "Turvatarkastuslisä"),
    "HANDLING_D": ("Airport handling, destination", "Määrälentoaseman käsittely"),
    "ROAD_FUEL": ("Fuel surcharge", "Polttoainelisä"),
    "FERRY": ("Ferry crossing", "Lauttamaksu"),
    "CASPIAN": ("Caspian Sea ferry and transshipment", "Kaspianmeren lautta ja uudelleenlastaus"),
    "TOLL": ("Road tolls", "Tiemaksut"),
    "TRAILER": ("Trailer hire, terminal tractor and port-area delivery", "Perävaunuvuokra, terminaalivetäjä ja satama-alueen jakelu"),
    "DG": ("Dangerous goods surcharge", "Vaarallisten aineiden lisä (VAK)"),
    "DOC_FEE": ("Documentation and booking management", "Asiakirjat ja varauksen hallinta"),
    "CUSTOMS": ("Import customs clearance (Tulli)", "Tuontitullaus (Tulli)"),
    "CUSTOMS_ITEMS": ("Additional customs items", "Lisätullinimikkeet"),
    "EXPORT_CUSTOMS": ("Export customs declaration", "Vienti-ilmoitus"),
    "ICS2": ("ICS2 entry summary declaration", "ICS2-saapumisen yleisilmoitus"),
    "INSURANCE": ("Cargo insurance", "Kuljetusvakuutus"),
    "DUTY": ("Customs duty (estimated)", "Tulli (arvio)"),
    "IMPORT_VAT": ("Import VAT (estimated)", "Tuonnin arvonlisävero (arvio)"),
    "MANUAL": ("Freight", "Rahti"),
    # s5 any-city quoting: estimate lines and surcharges from carrier notices
    "BORDER": ("Border crossing and transit documents", "Rajanylitys ja passitusasiakirjat"),
    "RAIL_LIFT": ("Rail terminal lift on/off", "Rautatieterminaalin nosto"),
    "COURIER": ("Express courier, door to door", "Pikakuriiri ovelta ovelle"),
    "WAR_RISK": ("War risk surcharge", "Sotariskilisä"),
    "WRS": ("War risk surcharge", "Sotariskilisä"),
    "PSS": ("Peak season surcharge (PSS)", "Sesonkilisä (PSS)"),
    "CAF": ("Currency adjustment factor (CAF)", "Valuuttakurssilisä (CAF)"),
}

LIABILITY = {
    "HAGUE_VISBY": {"limit_sdr_per_kg": 2.0, "limit_sdr_per_package": 667,
                    "label": "Hague-Visby / Finnish Maritime Code: 667 SDR per package or 2 SDR/kg, whichever is higher",
                    "label_fi": "Haagin-Visbyn säännöt / merilaki: 667 SDR kollilta tai 2 SDR/kg, kumpi on suurempi"},
    "CMR": {"limit_sdr_per_kg": 8.33, "label": "CMR art. 23: 8.33 SDR/kg", "label_fi": "CMR 23 art.: 8,33 SDR/kg"},
    "MONTREAL": {"limit_sdr_per_kg": 26.0, "label": "Montreal Convention: 26 SDR/kg", "label_fi": "Montrealin yleissopimus: 26 SDR/kg"},
    "CIM": {"limit_sdr_per_kg": 17.0, "label": "CIM (COTIF): 17 SDR/kg", "label_fi": "CIM (COTIF): 17 SDR/kg"},
    "NSAB": {"limit_sdr_per_kg": 8.33, "label": "NSAB 2015 (PSYM 2015): 8.33 SDR/kg", "label_fi": "NSAB 2015 (PSYM 2015): 8,33 SDR/kg"},
}


def line_text(code: str, default: str | None = None) -> tuple[str, str]:
    en, fi = LINE_TEXT.get(code, (default or code.replace("_", " ").title(), default or code.replace("_", " ").title()))
    return (default or en) if code not in LINE_TEXT else en, fi


# ------------------------------------------------------------------------------------------------
# Locations
# ------------------------------------------------------------------------------------------------
def _norm_loc(rec: dict | None) -> dict | None:
    if not rec or rec.get("lat") is None or rec.get("lon") is None:
        return None
    code = (rec.get("unlocode") or rec.get("locode") or "").upper() or None
    out = {"unlocode": code, "name": rec.get("name") or code, "country": (rec.get("country") or (code or "")[:2]).upper(),
           "lat": float(rec["lat"]), "lon": float(rec["lon"]), "kinds": list(rec.get("kinds") or [])}
    # city for summaries ("Tallinn"), name stays the precise point ("Tallinn Old City Harbour")
    out["city"] = rec.get("city") or out["name"]
    for field in ("subdivision", "territory_code"):
        if rec.get(field) is not None:
            out[field] = rec[field]
    if rec.get("address"):
        out["address"] = rec["address"]
    out["label"] = f"{out['name']} ({code}), {out['country']}" if code else f"{out['name']}, {out['country']}"
    return out


async def _v1_find(query: str) -> dict | None:
    try:
        from services import geo  # type: ignore  (V1 module, may not exist yet)

        return await geo.find_location(query)
    except Exception:  # noqa: BLE001
        return None


async def resolve_location(value) -> dict | None:
    """Location object | {unlocode} | {query} | "text" -> normalised location or None."""
    if not value and value != 0:
        return None
    if not isinstance(value, dict):
        value = {"query": str(value)[:200]}
    if value.get("lat") is not None and value.get("lon") is not None and (value.get("unlocode") or value.get("name")):
        try:
            lat, lon = float(value["lat"]), float(value["lon"])
        except (TypeError, ValueError):
            return None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or not value.get("country"):
            return None
        return place_loc(value)
    code = (value.get("unlocode") or value.get("locode") or "").strip().upper()
    query = (value.get("query") or value.get("name") or "").strip()
    from services import gazetteer

    await gazetteer.ready()  # cold indexes are built in a thread, never on the event loop
    if code:
        rec = await _v1_find(code) or await db.locations.find_one({"unlocode": code}, NO_ID)
        loc = _norm_loc(rec) or _norm_loc(net.network_location(code))
        if loc:
            return loc
    if query:
        if re.fullmatch(r"[A-Za-z]{2}[A-Za-z0-9]{3}", query):
            loc = await resolve_location({"unlocode": query})
            # Only a real code: "Lagos", "Paris", "Dubai" and "Turku" are five letters too, and the V1 finder answers
            # them with a name match (Lagos -> the NGLOA airport record) that the gazetteer below decides better.
            if loc and (loc.get("unlocode") or "").upper() == query.upper():
                return loc
        hits = await search_locations(query, limit=1, cities=False)
        existing = hits[0] if hits else None
        # s5: any city on earth. The gazetteer decides between the hub dataset's match and the city people mean.
        text = query
        cc = (value.get("country") or "").strip().upper()
        if cc and "," not in text and len(cc) == 2:
            text = f"{query}, {cc}"
        from services import gazetteer

        try:
            place = await gazetteer.resolve_text(text, existing)
        except gazetteer.GeocoderBusy:
            raise
        except Exception:  # noqa: BLE001 - the dataset answer still stands
            log.exception("gazetteer failed for %r", text)
            place = existing
        if place:
            return place_loc(place)
    return None


def place_loc(place: dict) -> dict | None:
    """_norm_loc plus the city facts the gazetteer knows (admin area, population, Finnish name, geocoder provenance)."""
    loc = _norm_loc(place)
    if not loc:
        return None
    for k in ("admin1", "population", "name_fi", "geocode", "geonameid"):
        if place.get(k) is not None:
            loc[k] = place[k]
    if not loc.get("unlocode"):
        loc["label"] = f"{loc['name']}, {loc['admin1']}, {loc['country']}" if loc.get("admin1") and place.get("geocode", {}).get("source") != "geonames" else f"{loc['name']}, {loc['country']}"
    return loc


async def search_locations(q: str, limit: int = 10, mode: str | None = None, cities: bool = True) -> list[dict]:
    """Autocomplete across V1's `locations` collection, the V2 rate network and (s5) the world city gazetteer."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    rx = re.escape(q)
    out: dict[str, dict] = {}
    try:
        cursor = db.locations.find(
            {"$or": [{"unlocode": {"$regex": f"^{rx}", "$options": "i"}}, {"name": {"$regex": f"^{rx}", "$options": "i"}},
                     {"aliases": {"$regex": f"^{rx}", "$options": "i"}}]}, NO_ID).limit(40)
        async for rec in cursor:
            loc = _norm_loc(rec)
            if loc and loc["unlocode"] not in out:
                out[loc["unlocode"] or loc["name"]] = loc
    except Exception:  # noqa: BLE001
        pass
    ql = q.lower()
    for code, rec in net.NETWORK_LOCATIONS.items():
        names = [rec["name"].lower(), code.lower()] + [a.lower() for a in rec.get("aliases", [])]
        if any(n.startswith(ql) or (len(ql) >= 3 and ql in n) for n in names) and code not in out:
            out[code] = _norm_loc(net.network_location(code))

    if cities:
        try:
            from services import gazetteer
            from services.hubs import haversine_km as _hk

            await gazetteer.ready()
            for c in gazetteer.suggest(q, limit=8):
                twin = next((x for x in out.values() if x.get("country") == c["country"] and _hk(x, c) <= 40
                             and (x.get("kinds") or []) != ["airport"]
                             and gazetteer.fold(x["name"]).startswith(gazetteer.fold(c["name"]))), None)
                if twin:
                    twin.setdefault("population", c["population"])  # the hub stands for the city: rank it like the city
                    continue
                loc = place_loc(c)
                if loc:
                    out[f"city:{c['geonameid']}"] = loc
        except Exception:  # noqa: BLE001
            log.exception("city suggestions failed for %r", q)

    def score(loc):
        s = 0
        if (loc.get("unlocode") or "").lower() == ql:
            s -= 100
        if loc["name"].lower().startswith(ql):
            s -= 20
        if code_in_network(loc.get("unlocode")):
            s -= 10
        if mode and mode_kind(mode) in loc.get("kinds", []):
            s -= 5
        if loc.get("country") == "FI":
            s -= 2
        s -= min(12.0, math.log10(max(1, loc.get("population") or 1)) * 2)  # Lagos NG before the Portuguese marina
        return (s, loc["name"])

    return sorted(out.values(), key=score)[:limit]


def code_in_network(code: str | None) -> bool:
    return bool(code and code in net.NETWORK_LOCATIONS)


def mode_kind(mode: str) -> str:
    return {"sea": "port", "air": "airport", "rail": "rail", "road": "road"}.get(mode, "city")


# ------------------------------------------------------------------------------------------------
# Cargo
# ------------------------------------------------------------------------------------------------
def _num(v, name: str, *, allow_none=True, minimum=0.0):
    if v is None or v == "":
        if allow_none:
            return None
        raise QuoteInputError(f"Enter the {name}.")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise QuoteInputError(f"The {name} must be a number.") from None
    if math.isnan(f) or f < minimum:
        raise QuoteInputError(f"The {name} must be {minimum:g} or more.")
    if f > 1e7:
        raise QuoteInputError(f"The {name} is too large to price online; ask our ops team.")
    return f


def normalize_cargo(cargo: dict | None) -> dict:
    c = dict(cargo or {})
    containers = []
    for row in c.get("containers") or []:
        ctype = str(row.get("type") or "").upper().replace("'", "").replace(" ", "")
        if ctype not in CONTAINERS:
            raise QuoteInputError(f"Unknown container type {row.get('type')!r}. Use 20GP, 40GP, 40HC, 45HC, 20RF or 40RF.")
        count = int(_num(row.get("count") or 1, "container count", minimum=1))
        if count > 50:
            raise QuoteInputError("More than 50 containers is priced by our ops team; send the request without the count.")
        containers.append({"type": ctype, "count": count})
    load_type = (c.get("load_type") or ("FCL" if containers else "loose")).upper()
    if load_type in ("LCL", "LTL", "FTL", "LOOSE"):
        load_type = "loose" if load_type != "FTL" else "FTL"
    if load_type == "FCL" and not containers:
        raise QuoteInputError("Add at least one container (type and count) for a full container load.")
    out = {
        "load_type": "FCL" if load_type == "FCL" else ("FTL" if load_type == "FTL" else "loose"),
        "description": (c.get("description") or "").strip()[:500] or None,
        "packages": int(_num(c.get("packages"), "number of packages") or 0) or None,
        "package_type": c.get("package_type") if c.get("package_type") in PACKAGE_LABEL else (c.get("package_type") and "other") or None,
        "weight_kg": _num(c.get("weight_kg"), "gross weight"),
        "volume_m3": _num(c.get("volume_m3"), "volume"),
        "ldm": _num(c.get("ldm"), "loading metres"),
        "stackable": bool(c.get("stackable")),
        "containers": containers if load_type == "FCL" else [],
        "dangerous_goods": bool(c.get("dangerous_goods") or c.get("dg_items")),
        "dg": c.get("dg") if c.get("dangerous_goods") else None,
        "dg_items": [dict(x) for x in c.get("dg_items") or [] if isinstance(x, dict)][:20] or None,
        "value_cents": int(_num(c.get("value_cents"), "cargo value") or 0) or None,
        "hs_hint": (c.get("hs_hint") or "").strip()[:20] or None,
        "customs_items": int(_num(c.get("customs_items"), "number of customs items") or 1),
        "duty_rate_pct": _num(c.get("duty_rate_pct"), "duty rate"),
        "vat_rate_pct": _num(c.get("vat_rate_pct"), "VAT rate"),
    }
    if out["dims"] if "dims" in out else False:
        pass
    if c.get("dims"):
        out["dims"] = c["dims"]
    if out["load_type"] != "FCL" and not out["weight_kg"]:
        raise QuoteInputError("Enter the total gross weight in kg.")
    if out["dg"]:
        dg = dict(out["dg"])
        if dg.get("un_number"):
            un = re.sub(r"\s+", "", str(dg["un_number"]).upper())
            un = un if un.startswith("UN") else f"UN{un}"
            if not re.fullmatch(r"UN\d{4}", un):
                raise QuoteInputError("The UN number must have four digits, for example UN1263.")
            dg["un_number"] = un
        out["dg"] = dg
    return out


def cargo_metrics(cargo: dict) -> dict:
    """Weight, volume, loading metres and container counts used by the chargeable rules."""
    w = cargo.get("weight_kg")
    v = cargo.get("volume_m3")
    ldm = cargo.get("ldm")
    if ldm is None and cargo.get("package_type") in PALLET_LDM and cargo.get("packages"):
        ldm = cargo["packages"] * PALLET_LDM[cargo["package_type"]] / (2 if cargo.get("stackable") else 1)
    containers = cargo.get("containers") or []
    teu = sum(CONTAINERS[c["type"]]["teu"] * c["count"] for c in containers)
    co2_weight_t = (w / 1000.0) if w else (teu * AVG_TONNES_PER_TEU if teu else 0.0)
    return {"weight_kg": w, "volume_m3": v, "ldm": round(ldm, 2) if ldm else None, "containers": containers, "teu": teu,
            "is_fcl": bool(containers), "co2_weight_t": co2_weight_t, "co2_weight_assumed": not w and bool(teu)}


def suggest_containers(weight_kg: float | None, volume_m3: float | None) -> list[dict]:
    w, v = weight_kg or 0.0, volume_m3 or 0.0
    if v <= CONTAINERS["20GP"]["cbm"] and w <= CONTAINERS["20GP"]["kg"]:
        return [{"type": "20GP", "count": 1}]
    n = max(1, math.ceil(max(v / CONTAINERS["40HC"]["cbm"], w / CONTAINERS["40HC"]["kg"])))
    return [{"type": "40HC", "count": n}]


def chargeable(service: str, m: dict) -> dict:
    w = m.get("weight_kg") or 0.0
    v = m.get("volume_m3") or 0.0
    ldm = m.get("ldm") or 0.0
    if service in ("AIR", "COURIER"):
        kg = max(w, v * (200 if service == "COURIER" else AIR_KG_PER_M3))
        kg = math.ceil(kg * 2) / 2  # IATA: round up to the next half kilo
        return {"basis": "PER_KG", "qty": kg, "chargeable_kg": kg, "label": f"{kg:,.1f} kg chargeable (1:{5000 if service == 'COURIER' else 6000})"}
    if service in ("ROAD_LTL", "HAULAGE_LTL"):
        kg = max(w, v * ROAD_KG_PER_M3, ldm * ROAD_KG_PER_LDM)
        kg = math.ceil(kg)
        return {"basis": "PER_KG", "qty": kg, "chargeable_kg": kg,
                "label": f"{kg:,.0f} kg chargeable (333 kg/m³, 1 850 kg/ldm)"}
    if service == "LCL":
        wm = max(w / LCL_KG_PER_WM, v, 1.0)
        wm = math.ceil(wm * 100) / 100
        return {"basis": "PER_WM", "qty": wm, "wm": wm, "label": f"{wm:.2f} W/M (1 m³ = 1 000 kg, min 1)"}
    if service in ("ROAD_FTL", "RORO", "HAULAGE_FTL"):
        trucks = max(1, math.ceil(max(ldm / TRAILER_LDM, w / TRAILER_KG, v / 85.0)))
        return {"basis": "PER_SHIPMENT", "qty": trucks, "vehicles": trucks, "label": f"{trucks} × full trailer (13.6 ldm)"}
    return {"basis": "PER_CONTAINER", "qty": sum(c["count"] for c in m.get("containers") or []),
            "label": ", ".join(f"{c['count']} × {CONTAINERS[c['type']]['label']}" for c in m.get("containers") or [])}


def cargo_summary(cargo: dict) -> str:
    parts = []
    if cargo.get("containers"):
        parts.append(", ".join(f"{c['count']} × {CONTAINERS[c['type']]['label']}" for c in cargo["containers"]))
    elif cargo.get("packages"):
        parts.append(f"{cargo['packages']} × {PACKAGE_LABEL.get(cargo.get('package_type') or 'other', 'package')}")
    if cargo.get("weight_kg"):
        parts.append(f"{cargo['weight_kg']:,.0f} kg".replace(",", " "))
    if cargo.get("volume_m3"):
        parts.append(f"{cargo['volume_m3']:.1f} m³")
    if cargo.get("dangerous_goods"):
        dg = cargo.get("dg") or {}
        parts.append("DG " + " ".join(x for x in [dg.get("un_number"), f"class {dg['class']}" if dg.get("class") else None] if x))
    return " · ".join(parts) or "—"


# ------------------------------------------------------------------------------------------------
# Request normalisation and validation
# ------------------------------------------------------------------------------------------------
def direction_of(o: dict, d: dict) -> str:
    if o["country"] == "FI" and d["country"] == "FI":
        return "domestic"
    if d["country"] == "FI":
        return "import"
    if o["country"] == "FI":
        return "export"
    return "cross_trade"


def _warn(code: str, message: str, level: str = "warning", **params) -> dict:
    return {"code": code, "level": level, "message": message, "params": params}


def check_incoterm(req: dict, *, strict: bool) -> tuple[str | None, list[dict]]:
    """RULE I-1, I-2, legacy terms, I-3, X-2, I-4. strict = portal/ops (named place required)."""
    warnings = []
    inc = (req.get("incoterm") or "").strip().upper() or None
    if inc in LEGACY_INCOTERMS:
        raise QuoteInputError(f"{inc} is not an Incoterms 2020 rule. Use {LEGACY_INCOTERMS[inc]} instead.")
    if inc and inc not in INCOTERMS_2020:
        raise QuoteInputError(f"{inc} is not an Incoterms 2020 rule. Use one of: {', '.join(INCOTERMS_2020)}.")
    mode = req.get("mode")
    if not inc:
        warnings.append(_warn("incoterm.missing", "No Incoterm given: the price covers the whole door-to-door chain. "
                                                  "Tell us the Incoterm so we quote only your part.", "info"))
        return None, warnings
    place = (req.get("incoterm_place") or "").strip()
    if not place:
        if strict:
            raise QuoteInputError(f"Name the place for {inc} (for example \"{inc} Shanghai port\"). Incoterms need a named place.")
        warnings.append(_warn("incoterm.place_missing", f"{inc} needs a named place.", incoterm=inc))
    if inc in SEA_ONLY_INCOTERMS:
        if mode and mode != "sea":
            raise QuoteInputError(f"{inc} applies to sea transport only. Use {'FCA' if inc in ('FAS', 'FOB') else ('CIP' if inc == 'CIF' else 'CPT')} for {mode}.")
        cargo = req.get("cargo") or {}
        if cargo.get("containers") or (mode in (None, "sea")):
            warnings.append(_warn("incoterm.container_sea_term",
                                  f"{inc} is meant for goods loaded on board at the port. For containers ICC recommends "
                                  f"{'FCA' if inc in ('FAS', 'FOB') else ('CIP' if inc == 'CIF' else 'CPT')}.", incoterm=inc))
    ins = (req.get("insurance") or {}).get("cover") if isinstance(req.get("insurance"), dict) else None
    if inc in ("CIF", "CIP") and (not ins or ins == "NONE"):
        warnings.append(_warn("incoterm.cif_cip_no_insurance", f"{inc} obliges the seller to insure the cargo; no insurance is recorded.",
                              "error", incoterm=inc))
    if inc == "CIP" and ins in ("ICC_B", "ICC_C"):
        warnings.append(_warn("incoterm.cip_needs_icc_a", "CIP requires cover equal to ICC (A); ICC (B) or (C) is not enough."))
    return inc, warnings


# RULE K-2 is checked on the raw input too: sanctioned places are often missing from the location data, and the
# customer must get the compliance answer, not "we could not find the place".
_SANCTIONED_NAMES = re.compile(r"\b(moscow|moskva|moskova|st\.? ?petersburg|saint petersburg|sankt[- ]peterburg|pietari|"
                               r"kaliningrad|novorossiysk|ust[- ]luga|vladivostok|vyborg|viipuri|minsk|vitebsk|hrodna|grodno|"
                               r"russia|venäjä|belarus|valko-?venäjä)\b", re.I)


def _sanctioned_input(value) -> bool:
    if isinstance(value, dict):
        if str(value.get("country") or "").upper() in net.SANCTIONED_COUNTRIES:
            return True
        value = value.get("unlocode") or value.get("name") or ""
    text = str(value or "").strip()
    if re.fullmatch(r"(RU|BY)\s?[A-Z0-9]{3}", text.upper()):
        return True
    return bool(_SANCTIONED_NAMES.search(text))


async def normalize_request(request: dict, *, strict: bool = False) -> dict:
    req = dict(request or {})
    if _sanctioned_input(req.get("origin")) or _sanctioned_input(req.get("destination")):
        raise QuoteInputError("Shipments to or from Russia and Belarus need a compliance review and are not quoted "
                              "online. Contact ops@huolitsija.fi.")
    from services.gazetteer import GeocoderBusy

    try:
        origin = await resolve_location(req.get("origin"))
    except GeocoderBusy:
        raise QuoteInputError("We could not look up the origin right now: the map service is busy. Pick a place from "
                              "the list, or try again in a minute.") from None
    if not origin:
        raise QuoteInputError("We could not find the origin. Pick a city, port or UN/LOCODE from the list.")
    try:
        dest = await resolve_location(req.get("destination"))
    except GeocoderBusy:
        raise QuoteInputError("We could not look up the destination right now: the map service is busy. Pick a place "
                              "from the list, or try again in a minute.") from None
    if not dest:
        raise QuoteInputError("We could not find the destination. Pick a city, port or UN/LOCODE from the list.")
    for loc in (origin, dest):
        if loc["country"] in net.SANCTIONED_COUNTRIES:
            raise QuoteInputError("Shipments to or from Russia and Belarus need a compliance review and are not quoted "
                                  "online. Contact ops@huolitsija.fi.")
    if origin.get("unlocode") and origin.get("unlocode") == dest.get("unlocode") or (
            abs(origin["lat"] - dest["lat"]) < 1e-4 and abs(origin["lon"] - dest["lon"]) < 1e-4):
        raise QuoteInputError("Origin and destination are the same place.")
    mode = (req.get("mode") or "").lower() or None
    if mode in ("any", "all"):
        mode = None
    if mode and mode not in ("sea", "road", "air", "rail"):
        raise QuoteInputError("Mode must be sea, road, air or rail.")
    req["mode"] = mode
    req["origin"], req["destination"] = origin, dest
    req["cargo"] = normalize_cargo(req.get("cargo"))
    rd = req.get("ready_date")
    today = now().date()
    input_warnings = []
    if rd:
        try:
            ready = date.fromisoformat(str(rd)[:10])
        except ValueError:
            raise QuoteInputError("Ready date must be a date (YYYY-MM-DD).") from None
        if ready < today:
            input_warnings.append(_warn("ready_date.moved", f"The ready date {ready.isoformat()} is in the past; "
                                                            f"prices are for cargo ready today ({today.isoformat()}).",
                                        "info", requested=ready.isoformat(), used=today.isoformat()))
            ready = today
        if ready > today + timedelta(days=180):
            raise QuoteInputError("We quote shipments ready within the next 6 months.")
    else:
        ready = today + timedelta(days=1)
        while ready.weekday() >= 5:
            ready += timedelta(days=1)
    req["ready_date"] = ready.isoformat()
    req["direction"] = direction_of(origin, dest)
    role = req.get("customer_role")
    if role not in ("buyer", "seller"):
        role = "seller" if req["direction"] == "export" else "buyer"
    req["customer_role"] = role
    ins = req.get("insurance")
    if isinstance(ins, bool):
        ins = {"cover": "ICC_A" if ins else "NONE"}
    ins = dict(ins or {})
    cover = (ins.get("cover") or "NONE").upper()
    if cover not in ("NONE", "ICC_A", "ICC_B", "ICC_C"):
        raise QuoteInputError("Insurance cover must be NONE, ICC_A, ICC_B or ICC_C.")
    req["insurance"] = {"cover": cover}
    customs_border.apply(req)
    inc, inc_warnings = check_incoterm(req, strict=strict)
    req["incoterm"] = inc
    req["incoterm_place"] = (req.get("incoterm_place") or "").strip() or None
    c = req["cargo"]
    if c.get("packages") and c.get("weight_kg") and not c.get("containers"):
        per = c["weight_kg"] / c["packages"]
        floor = {"pallet_eur": 5.0, "pallet_fin": 5.0}.get(c.get("package_type"), 0.05)
        if per < floor:
            input_warnings.append(_warn("cargo.check_weight", f"{c['weight_kg']:g} kg for {c['packages']} packages is "
                                                              f"{per:.2f} kg each; check the total gross weight.",
                                        per_package_kg=round(per, 2)))
    req["_warnings"] = input_warnings + inc_warnings
    return req


# ------------------------------------------------------------------------------------------------
# Rate cards
# ------------------------------------------------------------------------------------------------
def card_valid_on(card: dict, day: date) -> bool:
    vf = card.get("valid_from")
    vt = card.get("valid_to")
    return (not vf or date.fromisoformat(vf[:10]) <= day) and (not vt or day <= date.fromisoformat(vt[:10]))


# Coordinates of rate-network nodes from V1's `locations` collection (same points the map uses), loaded per request.
# unlocode -> {"primary": {lat, lon}, "port": {...}, "airport": {...}}. One UN/LOCODE can name several places
# (FIHEL: Vuosaari harbour and Helsinki-Vantaa airport); a sea leg must end at the port, an air leg at the airport.
_LOC_OVERRIDE: dict[str, dict] = {}
_KIND_FOR_MODE = {"sea": "port", "air": "airport"}


async def load_network_coordinates(codes: set[str]):
    missing = [c for c in codes if c and c not in _LOC_OVERRIDE]
    if not missing:
        return
    try:
        found: dict[str, list[dict]] = {}
        async for rec in db.locations.find({"unlocode": {"$in": missing}},
                                           {"_id": 0, "unlocode": 1, "code": 1, "kinds": 1, "rank": 1, "lat": 1, "lon": 1}):
            if rec.get("lat") is not None and rec.get("lon") is not None:
                found.setdefault(rec["unlocode"], []).append(rec)
        for code, recs in found.items():
            _LOC_OVERRIDE[code] = _coordinates_by_kind(code, recs)
    except Exception:  # noqa: BLE001
        pass


def _coordinates_by_kind(code: str, recs: list[dict]) -> dict:
    """Primary record as services.geo picks it (code == UN/LOCODE, else highest rank), plus the best port and airport."""
    def xy(r):
        return {"lat": float(r["lat"]), "lon": float(r["lon"])}

    ranked = sorted(recs, key=lambda r: (r.get("code") == code, r.get("rank") or 0), reverse=True)
    out = {"primary": xy(ranked[0])}
    for kind in ("port", "airport"):
        hit = next((r for r in ranked if kind in (r.get("kinds") or [])), None)
        if hit:
            out[kind] = xy(hit)
    return out


def endpoint_loc(ep: dict, mode: str | None = None) -> dict | None:
    if not ep:
        return None
    if ep.get("lat") is not None and ep.get("lon") is not None:
        return {"unlocode": ep.get("unlocode"), "name": ep.get("name"), "country": ep.get("country") or (ep.get("unlocode") or "")[:2],
                "lat": ep["lat"], "lon": ep["lon"], "kinds": []}
    if ep.get("unlocode"):
        base = net.network_location(ep["unlocode"])
        if base:
            ov = _LOC_OVERRIDE.get(ep["unlocode"], {})
            return {**base, **(ov.get(_KIND_FOR_MODE.get(mode)) or ov.get("primary") or {}), "name": ep.get("name") or base["name"]}
        if ep.get("lat") is not None:
            return {"unlocode": ep["unlocode"], "name": ep.get("name"), "country": ep["unlocode"][:2],
                    "lat": ep["lat"], "lon": ep["lon"], "kinds": []}
    return None


def endpoint_countries(ep: dict) -> set[str]:
    if ep.get("countries"):
        return {c.upper() for c in ep["countries"]}
    if ep.get("country"):
        return {ep["country"].upper()}
    if ep.get("unlocode"):
        return {ep["unlocode"][:2].upper()}
    return set()


def is_region(ep: dict) -> bool:
    return not ep.get("unlocode") and ep.get("lat") is None


def service_family(service: str) -> str:
    return {"FCL": "fcl", "RAIL_FCL": "fcl", "HAULAGE": "fcl", "LCL": "loose", "AIR": "loose", "ROAD_LTL": "loose",
            "ROAD_FTL": "ftl", "RORO": "ftl", "RAIL_WAGON": "ftl"}.get(service, "loose")


def _tier_unit(card: dict, qty: float) -> float:
    """Unit cost in cents for the weight/volume break. Per-kg road tariffs carry sub-cent precision (13.5 c/kg)."""
    tiers = sorted(card.get("tiers") or [], key=lambda t: t.get("max_qty") or 1e18)
    for t in tiers:
        if t.get("max_qty") is None or qty <= t["max_qty"]:
            return float(t["unit_cost_cents"])
    if tiers:
        return float(tiers[-1]["unit_cost_cents"])
    return float(card.get("unit_cost_cents") or 0)


def _distance_factor(card: dict, km: float) -> float:
    for band in sorted(card.get("distance_bands") or [], key=lambda b: b.get("max_km") or 1e18):
        if band.get("max_km") is None or km <= band["max_km"]:
            return float(band.get("factor") or 1.0)
    bands = card.get("distance_bands") or []
    return float(bands[-1]["factor"]) if bands else 1.0


def _cost_line(code, stage, basis, qty, unit_cost_cents, *, description=None, note=None, leg_seq=None) -> dict:
    en, fi = line_text(code, description)
    if description and code not in LINE_TEXT:
        en = fi = description
    return {"code": code, "description": en, "description_fi": fi, "stage": stage, "basis": basis,
            "qty": round(float(qty), 3), "unit_cost_cents": int(round(unit_cost_cents)), "is_pass_through": False,
            "estimated": False, "note": note, "leg_seq": leg_seq}


def price_card(card: dict, m: dict, *, km: float, kind: str, leg_seq: int, freight_code: str) -> list[dict]:
    """Cost lines for one rate card and one cargo. kind: pre|main|connection|on."""
    if card.get("cargo_scope") and any(m.get(k) != value for k, value in card["cargo_scope"].items()):
        return []  # a spot offer for one cargo cannot price a different consignment
    service = card["service"]
    basis = card.get("basis")
    stage_default = {"pre": "origin", "on": "destination"}.get(kind, "main")
    lines: list[dict] = []
    by_size_count: dict[str, int] = {}
    if service_family(service) == "fcl":
        for c in m["containers"]:
            by_size_count[CONTAINERS[c["type"]]["size"]] = by_size_count.get(CONTAINERS[c["type"]]["size"], 0) + c["count"]
    ch = chargeable("HAULAGE_LTL" if service == "ROAD_LTL" else service, m)
    vehicles = ch.get("vehicles", 1)
    base_total = 0

    if basis == "PER_CONTAINER":
        costs = card.get("container_costs") or {}
        for c in m["containers"]:
            unit = costs.get(c["type"])
            if unit is None and c["type"] == "45HC" and costs.get("40HC"):
                unit = int(costs["40HC"] * 1.12)
            if unit is None and c["type"] == "40GP" and costs.get("40HC"):
                unit = costs["40HC"]
            if unit is None and c["type"] == "40HC" and costs.get("40GP"):
                unit = costs["40GP"]
            if unit is None:
                return []  # equipment not offered on this card (e.g. reefers)
            ln = _cost_line(freight_code, stage_default, "PER_CONTAINER", c["count"], unit, leg_seq=leg_seq,
                            note=CONTAINERS[c["type"]]["label"])
            lines.append(ln)
            base_total += unit * c["count"]
    elif basis == "PER_KM":
        unit = int(card.get("unit_cost_cents") or 0) * _distance_factor(card, km)
        units = vehicles if service_family(service) != "fcl" else sum(by_size_count.values())
        amount = unit * km * units
        minimum = (card.get("min_cost_cents") or 0) * units
        note = f"{km:,.0f} km".replace(",", " ")
        if amount < minimum:
            lines.append(_cost_line(freight_code, stage_default, "PER_SHIPMENT", units, minimum / units, leg_seq=leg_seq,
                                    note=f"{note}, minimum charge"))
            base_total = minimum
        else:
            lines.append(_cost_line(freight_code, stage_default, "PER_KM", km * units, unit, leg_seq=leg_seq,
                                    note=f"{note} × {units}" if units > 1 else note))
            base_total = amount
    elif basis == "PER_SHIPMENT":
        unit = int(card.get("unit_cost_cents") or 0) * _distance_factor(card, km)
        lines.append(_cost_line(freight_code, stage_default, "PER_SHIPMENT", vehicles, unit, leg_seq=leg_seq,
                                note=ch.get("label")))
        base_total = unit * vehicles
    elif basis == "PER_LDM":
        qty = vehicles * TRAILER_LDM
        unit = _tier_unit(card, qty)
        lines.append(_cost_line(freight_code, stage_default, "PER_LDM", qty, unit, leg_seq=leg_seq,
                                note=f"{vehicles} × 13.6 m trailer"))
        base_total = unit * qty
    else:  # PER_KG / PER_WM
        if service == "AIR":
            ch = chargeable("AIR", m)
        elif service == "LCL":
            ch = chargeable("LCL", m)
        qty = ch["qty"]
        unit = _tier_unit(card, qty) * _distance_factor(card, km)
        amount = unit * qty
        minimum = card.get("min_cost_cents") or 0
        if amount < minimum:
            lines.append(_cost_line(freight_code, stage_default, "PER_SHIPMENT", 1, minimum, leg_seq=leg_seq,
                                    note=f"{ch['label']}, minimum charge"))
            base_total = minimum
        else:
            note = ch["label"] + (f", {km:,.0f} km".replace(",", " ") if card.get("distance_bands") else "")
            if (basis or ch["basis"]) == "PER_KG" and service != "AIR":
                # Road/rail tariffs are quoted per 100 kg: whole cents per unit keep 0.0001 EUR/kg precision on the
                # quote and the invoice (a per-kg unit in whole cents would round 13.5 c/kg to 14 c/kg).
                lines.append(_cost_line(freight_code, stage_default, "PER_100KG", qty / 100.0, unit * 100.0, leg_seq=leg_seq,
                                        note=note))
            else:
                lines.append(_cost_line(freight_code, stage_default, basis or ch["basis"], qty, unit, leg_seq=leg_seq, note=note))
            base_total = amount

    for s in card.get("surcharges") or []:
        code = s.get("code") or "SURCHARGE"
        stage = STAGE_OF_CODE.get(code, "main") if kind in ("main", "connection") else stage_default
        if kind == "connection" and code == "OTHC":
            continue  # the hub load is in the connecting carrier's freight
        sb = s.get("basis") or "PER_SHIPMENT"
        label = s.get("label")
        if sb == "PERCENT":
            pct = float(s.get("pct") or 0)
            if not pct:
                continue
            amt = base_total * pct / 100
            lines.append(_cost_line(code, stage, "PERCENT", 1, amt, leg_seq=leg_seq, note=f"{pct:g} % of freight",
                                    description=label if code not in LINE_TEXT else None))
            continue
        if sb == "PER_CONTAINER":
            if not by_size_count:
                continue
            for size, count in sorted(by_size_count.items()):
                unit = (s.get("by_size") or {}).get("40" if size == "45" else size, s.get("unit_cost_cents"))
                if unit:
                    lines.append(_cost_line(code, stage, "PER_CONTAINER", count, unit, leg_seq=leg_seq, note=f"{size}'",
                                            description=label if code not in LINE_TEXT else None))
            continue
        if sb == "PER_WM":
            qty = chargeable("LCL", m)["qty"]
        elif sb == "PER_KG":
            qty = chargeable(service, m)["qty"] if service in ("AIR", "COURIER") else chargeable("HAULAGE_LTL", m)["qty"]
        elif sb == "PER_LDM":
            qty = vehicles * TRAILER_LDM
        elif sb == "PER_TRUCK":
            qty = vehicles
        else:  # PER_SHIPMENT / PER_BL
            qty = 1
        unit = s.get("unit_cost_cents") or 0
        amt = unit * qty
        if s.get("min_cents") and amt < s["min_cents"]:
            lines.append(_cost_line(code, stage, "PER_SHIPMENT", 1, s["min_cents"], leg_seq=leg_seq, note="minimum",
                                    description=label if code not in LINE_TEXT else None))
        elif unit:
            lines.append(_cost_line(code, stage, sb if sb != "PER_TRUCK" else "PER_SHIPMENT", qty, unit, leg_seq=leg_seq,
                                    description=label if code not in LINE_TEXT else None))
    return lines


# ------------------------------------------------------------------------------------------------
# Route building
# ------------------------------------------------------------------------------------------------
# Short-sea / ferry crossings inside road and rail routings: their distance is sailed, not driven.
FERRY_PAIRS = {frozenset(p) for p in [("DETRV", "FIHEL"), ("EETLL", "FIHEL"), ("SESTO", "FITKU"), ("SESTO", "FIHEL"),
                                      ("SEKPS", "FINLI"), ("EEPLA", "SEKPS"), ("DERSK", "FIHKO")]}
# Groupage delivery/collection zones: factor on the linehaul rate by road distance from the terminal.
ZONE_BANDS = [{"max_km": 60, "factor": 1.0}, {"max_km": 150, "factor": 1.12}, {"max_km": 300, "factor": 1.25},
              {"max_km": 500, "factor": 1.45}, {"max_km": 800, "factor": 1.7}, {"max_km": None, "factor": 2.0}]
MAX_ZONE_KM = 1200
MAX_UNPRICED_PICKUP_KM = 100


def eligible_services(m: dict, mode: str | None, cargo: dict) -> tuple[set[str], list[dict]]:
    """Services that can carry this cargo, plus an FCL alternative for large loose cargo."""
    extra = []
    if m["is_fcl"]:
        services = {"FCL", "RAIL_FCL", "HAULAGE"}
    else:
        services = {"LCL", "AIR", "ROAD_LTL"}
        w, v, ldm = m["weight_kg"] or 0, m["volume_m3"] or 0, m["ldm"] or 0
        if ldm >= 6 or w >= 10000 or v >= 40 or cargo.get("load_type") == "FTL":
            services |= {"ROAD_FTL", "RORO"}
        if ldm >= 10 or w >= 20000 or v >= 70:
            services.discard("ROAD_LTL")
        if w > 15000 or v > 80:
            services.discard("AIR")
        if v >= 14 or w >= 10000:
            extra.append({"containers": suggest_containers(m["weight_kg"], m["volume_m3"])})
            services |= {"FCL"}
    if mode:
        services = {s for s in services if MODE_OF_SERVICE.get(s) == mode}
    return services, extra


def _zone_factor(km: float) -> float:
    for b in ZONE_BANDS:
        if b["max_km"] is None or km <= b["max_km"]:
            return b["factor"]
    return ZONE_BANDS[-1]["factor"]


def cover(place: dict, card: dict, side: str) -> tuple[str, float] | None:
    """How a card endpoint serves a requested place: ("direct", 0) inside the included radius or a region card,
    ("zone", road km) for groupage terminals delivering country-wide, ("haul", km) needs pre/on-carriage in the
    same country, None when it cannot serve the place."""
    ep = card[side]
    if is_region(ep):
        return ("direct", 0.0) if place["country"] in endpoint_countries(ep) else None
    loc = endpoint_loc(ep, card.get("mode"))
    if not loc:
        return None
    if place.get("unlocode") and place["unlocode"] == loc["unlocode"]:
        return ("direct", 0.0)
    d = net.haversine_km(place, loc)
    if d <= float(ep.get("radius_km") or 25):
        return ("direct", 0.0)
    if place["country"] != loc["country"]:
        return None
    if card["service"] == "ROAD_LTL" and d * net.CIRCUITY["road"] <= MAX_ZONE_KM:
        return ("zone", round(net.leg_distance_km("road", loc, place), 1))
    return ("haul", d)


# backwards-compatible helper used by carrier ranking
def _place_match(place: dict, ep: dict, ep_loc: dict | None) -> float | None:
    if is_region(ep):
        return 0.0 if place["country"] in endpoint_countries(ep) else None
    if not ep_loc:
        return None
    if place.get("unlocode") and place["unlocode"] == ep_loc["unlocode"]:
        return 0.0
    d = net.haversine_km(place, ep_loc)
    return 0.0 if d <= float(ep.get("radius_km") or 25) else d


def routed_segments(mode: str, points: list[dict]) -> list[dict]:
    """Split a card's routing (from, via..., to) into driven/railed and sailed segments."""
    segs = []
    for a, b in zip(points, points[1:]):
        if not a or not b:
            continue
        pair = frozenset((a.get("unlocode"), b.get("unlocode")))
        seg_mode = "sea" if (mode in ("road", "rail") and pair in FERRY_PAIRS) else mode
        km = net.leg_distance_km(seg_mode, a, b)
        segs.append({"mode": seg_mode, "km": round(km, 1), "from": a.get("unlocode") or a.get("name"),
                     "to": b.get("unlocode") or b.get("name")})
    return segs


def _via_points(card: dict) -> list[dict]:
    pts = []
    for code in card.get("via") or []:
        loc = endpoint_loc({"unlocode": code}, card.get("mode"))
        if loc:
            pts.append(loc)
    return pts


async def build_options(req: dict, m: dict, settings: dict, company: dict | None) -> tuple[list[dict], list[dict], list[dict]]:
    """Enumerate routes over the rate graph and price them. Returns (options, not_quoted, warnings)."""
    origin, dest = req["origin"], req["destination"]
    ready = date.fromisoformat(req["ready_date"])
    services, extras = eligible_services(m, req.get("mode"), req["cargo"])
    warnings: list[dict] = []
    not_quoted: list[dict] = []

    carriers = {c["id"]: c async for c in db.carriers.find({"active": {"$ne": False}}, NO_ID)}
    all_cards = [c async for c in db.rate_cards.find({"archived": {"$ne": True}, "status": {"$nin": ["pending", "rejected", "revoked"]}}, NO_ID)]
    cards = [c for c in all_cards if c.get("carrier_id") in carriers and not str(c.get("service") or "").startswith("AGENT_")]
    codes = set()
    for c in cards:
        codes |= {c["origin"].get("unlocode"), c["destination"].get("unlocode"), *(c.get("via") or [])}
    await load_network_coordinates({x for x in codes if x})
    live = [c for c in cards if card_valid_on(c, ready)]
    expired_lane = [c for c in cards if not card_valid_on(c, ready)]
    region_cards = [c for c in live if is_region(c["origin"]) and is_region(c["destination"])]
    lane_cards = [c for c in live if not (is_region(c["origin"]) and is_region(c["destination"]))]

    variants = [m]
    if not m["is_fcl"] and "FCL" in services and extras:
        alt = dict(m)
        alt.update(containers=extras[0]["containers"], is_fcl=True,
                   teu=sum(CONTAINERS[c["type"]]["teu"] * c["count"] for c in extras[0]["containers"]))
        variants = [m, alt]
        lbl = ", ".join(f"{c['count']} × {CONTAINERS[c['type']]['label']}" for c in extras[0]["containers"])
        warnings.append(_warn("cargo.fcl_suggested", f"At this volume a full container ({lbl}) may be cheaper than groupage.",
                              "info", containers=lbl))

    def haul_cost(country: str, a: dict, b: dict, family: str, mm: dict):
        """Cheapest domestic tariff for a pre/on-carriage leg inside one country -> (card, lines, km) or None."""
        km = net.leg_distance_km("road", a, b)
        if km > MAX_HAUL_KM["fcl" if family == "fcl" else "loose"]:
            return None
        want = {"fcl": {"HAULAGE"}, "ftl": {"ROAD_FTL"}, "loose": {"ROAD_LTL"}}[family]
        best = None
        for c in region_cards:
            if c["service"] not in want or country not in endpoint_countries(c["origin"]) or country not in endpoint_countries(c["destination"]):
                continue
            lines = price_card(c, mm, km=km, kind="pre", leg_seq=0, freight_code="PICKUP")
            if not lines:
                continue
            cost = sum(ln["unit_cost_cents"] * ln["qty"] for ln in lines)
            if best is None or cost < best[2]:
                best = (c, lines, cost)
        return (best[0], best[1], km) if best else None

    paths = []
    for mm in variants:
        for card in live:
            svc = card["service"]
            if svc not in services:
                continue
            fam = service_family(svc)
            if (fam == "fcl") != bool(mm["is_fcl"]):
                continue
            region = card in region_cards
            if svc == "HAULAGE" and not (region and origin["country"] == dest["country"]):
                continue  # container trucking is only a main leg for domestic container moves
            o_cov = cover(origin, card, "origin")
            if o_cov is None:
                continue
            if region:
                if cover(dest, card, "destination"):
                    paths.append((mm, card, o_cov, None, ("direct", 0.0)))
                continue
            o_loc = endpoint_loc(card["origin"], card.get("mode"))
            if o_cov[0] == "haul" and o_cov[1] > MAX_HAUL_KM["fcl" if fam == "fcl" else "loose"]:
                continue
            d_cov = cover(dest, card, "destination")
            d_loc = endpoint_loc(card["destination"], card.get("mode"))
            if d_cov and d_cov[0] in ("direct", "zone"):
                paths.append((mm, card, o_cov, None, d_cov))
                continue
            if d_cov and d_cov[1] <= MAX_HAUL_KM["fcl" if fam == "fcl" else "loose"]:
                paths.append((mm, card, o_cov, None, d_cov))
            if fam != "fcl" or not d_loc:
                continue
            # connection at the card's destination hub (feeder / rail), must bring the cargo closer
            here = net.haversine_km(d_loc, dest)
            for c2 in lane_cards:
                if c2["id"] == card["id"] or c2["origin"].get("unlocode") != d_loc["unlocode"]:
                    continue
                if c2["service"] not in ("FCL", "RAIL_FCL") or c2["destination"].get("unlocode") == (o_loc or {}).get("unlocode"):
                    continue
                c2_loc = endpoint_loc(c2["destination"], c2.get("mode"))
                if not c2_loc or net.haversine_km(c2_loc, dest) > 0.7 * here:
                    continue
                d2 = cover(dest, c2, "destination")
                if d2 is None or (d2[0] == "haul" and d2[1] > MAX_HAUL_KM["fcl"]):
                    continue
                paths.append((mm, card, o_cov, c2, d2))

    seen: dict[str, dict] = {}
    for mm, card, o_cov, c2, d_cov in paths:
        opt = await _price_path(req, mm, card, o_cov, c2, d_cov, carriers, haul_cost, settings, company)
        if opt is None:
            continue
        if opt.get("_not_quoted"):
            if opt["_not_quoted"] not in not_quoted:
                not_quoted.append(opt["_not_quoted"])
            continue
        key = opt["_dedupe"]
        if key in seen and seen[key]["total_cents"] <= opt["total_cents"]:
            continue
        seen[key] = opt
    options = list(seen.values())
    for o in options:
        o.pop("_dedupe", None)

    # Cards that would serve this lane but are outside their validity: say so (ops can refresh them).
    for c in expired_lane:
        if c["service"] not in services or is_region(c["origin"]):
            continue
        oc, dc = cover(origin, c, "origin"), cover(dest, c, "destination")
        if oc and dc and oc[0] != "haul" and dc[0] != "haul":
            car = carriers.get(c["carrier_id"], {})
            vt, vf = c.get("valid_to"), c.get("valid_from")
            reason = (f"Rate expired on {vt[:10]}" if vt and date.fromisoformat(vt[:10]) < ready else f"Rate valid from {vf[:10]}")
            not_quoted.append({"carrier_name": car.get("name"), "mode": c["mode"], "service_name": c.get("service_name"),
                               "reason": reason, "code": "rate_validity"})
    return options, not_quoted, warnings


def _next_departure(day: date, weekdays: list[int] | None) -> date:
    if not weekdays:
        return day
    for i in range(0, 14):
        d = day + timedelta(days=i)
        if d.weekday() in weekdays:
            return d
    return day


FREIGHT_CODE = {"FCL": "OCEAN_FREIGHT", "LCL": "OCEAN_FREIGHT", "AIR": "AIR_FREIGHT", "ROAD_LTL": "ROAD_FREIGHT",
                "ROAD_FTL": "ROAD_FREIGHT", "RORO": "RORO_FREIGHT", "RAIL_FCL": "RAIL_FREIGHT", "RAIL_WAGON": "RAIL_FREIGHT",
                "HAULAGE": "ROAD_FREIGHT"}


def ferry_coverage(card: dict, lines: list[dict]) -> dict:
    """Carry structured purchase terms, never infer inclusion from a service name or route geometry.

    Partner RFQs ask for every mandatory charge (including ferries); accepted partner cards persist that
    all-in purchase total. A priced FERRY line also purchases the crossing. Other tariffs need a person.
    """
    proof = {"rate_card_id": card.get("id"), "outreach_id": card.get("outreach_id")}
    if card.get("ferry_included") is False or card.get("all_in") is False:
        return {**proof, "included": False, "source": "rate_card_exclusion"}
    if card.get("source") == "partner_network" and card.get("status") == "accepted" \
            and type(card.get("all_in_cost_cents")) is int and card["all_in_cost_cents"] > 0:
        return {**proof, "included": True, "source": "accepted_partner_all_in"}
    if any(ln.get("code") == "FERRY" and not ln.get("is_pass_through") and type(ln.get("unit_cost_cents")) is int
           and ln["unit_cost_cents"] > 0 for ln in lines):
        return {**proof, "included": True, "source": "priced_ferry_line"}
    return {**proof, "included": None, "source": "unproven"}


def _co2_parts(segments: list[dict], main_factor: float, card_mode: str) -> list[tuple[float, float]]:
    parts = []
    for s in segments:
        f = main_factor if s["mode"] == card_mode else net.DEFAULT_CO2_G_TKM["RORO"]
        parts.append((s["km"], f))
    return parts


async def _price_path(req, m, card, o_cov, c2, d_cov, carriers, haul_cost, settings, company):
    origin, dest = req["origin"], req["destination"]
    carrier = carriers[card["carrier_id"]]
    cargo = req["cargo"]
    fam = service_family(card["service"])
    if cargo.get("dangerous_goods"):  # RULE D-2 for every carrier on the route
        for c in [card] + ([c2] if c2 else []):
            ok, reason = net.dg_acceptance(carriers[c["carrier_id"]], cargo.get("dg") or {})
            if not ok:
                return {"_not_quoted": {"carrier_name": carriers[c["carrier_id"]]["name"], "mode": c["mode"],
                                        "service_name": c.get("service_name"), "reason": reason, "code": "dg_not_accepted"}}
    region = is_region(card["origin"]) and is_region(card["destination"])
    o_loc, d_loc = endpoint_loc(card["origin"], card.get("mode")), endpoint_loc(card["destination"], card.get("mode"))
    haul_family = "fcl" if fam == "fcl" else ("ftl" if fam == "ftl" else "loose")
    legs, lines, partial = [], [], []
    seq = 1

    # --- pre-carriage -------------------------------------------------------------------------------
    if o_cov[0] == "haul":
        h = haul_cost(origin["country"], origin, o_loc, haul_family, m)
        if h:
            hc, hl, km = h
            hcar = carriers.get(hc["carrier_id"], {})
            en, fi = line_text("PICKUP_FI" if origin["country"] == "FI" else "PICKUP")
            for ln in hl:
                ln.update(leg_seq=seq, stage="origin")
                if ln["code"] == "PICKUP":
                    ln.update(description=en, description_fi=fi)
            lines += hl
            legs.append({"seq": seq, "kind": "pre_carriage", "role": "pickup", "mode": "road", "from": _leg_loc(origin), "to": _leg_loc(o_loc),
                         "carrier_id": hc["carrier_id"], "carrier_name": hcar.get("name"), "transit_days": 1,
                         "distance_km": round(km), "service": hc["service"], "rate_card_id": hc["id"],
                         "_co2_parts": [(km, hcar.get("co2_factor_g_tkm") or net.DEFAULT_CO2_G_TKM["HAULAGE"])],
                         "_valid_to": hc.get("valid_to")})
            seq += 1
        elif card["mode"] == "air" and o_cov[1] <= MAX_UNPRICED_PICKUP_KM:
            partial.append({"code": "PICKUP", "description": f"Pickup {origin['name']} → {o_loc['name']}",
                            "description_fi": f"Nouto {origin['name']} → {o_loc['name']}", "amount_cents": None,
                            "reason": "Priced on request (no local tariff loaded)", "reason_code": "pickup.on_request"})
        else:
            return None

    # --- main leg -------------------------------------------------------------------------------------
    main_from = origin if (region or o_cov[0] == "zone") else o_loc
    main_to = dest if (region or (not c2 and d_cov[0] == "zone")) else d_loc
    if region:
        points = [origin] + _via_points(card) + [dest]
    else:
        points = ([origin] if o_cov[0] == "zone" else []) + [o_loc] + _via_points(card) + [d_loc] + (
            [dest] if (not c2 and d_cov[0] == "zone") else [])
    segs = routed_segments(card["mode"], points)
    if card.get("distance_km"):
        segs = [{"mode": card["mode"], "km": float(card["distance_km"]), "from": main_from.get("unlocode"), "to": main_to.get("unlocode")}]
    km_main = sum(s["km"] for s in segs)
    driven_km = sum(s["km"] for s in segs if s["mode"] == card["mode"]) * float(card.get("circuity") or 1.0)
    zone_km = (o_cov[1] if o_cov[0] == "zone" else 0.0) + (d_cov[1] if (not c2 and d_cov[0] == "zone") else 0.0)
    zfac = (_zone_factor(o_cov[1]) if o_cov[0] == "zone" else 1.0) * (
        _zone_factor(d_cov[1]) if (not c2 and d_cov[0] == "zone") else 1.0)
    price_km = driven_km if card["mode"] == "road" else km_main
    ml = price_card(card, m, km=price_km, kind="main", leg_seq=seq, freight_code=FREIGHT_CODE.get(card["service"], "MANUAL"))
    if not ml:
        return None
    if zfac != 1.0:
        for ln in ml:
            if ln["code"] == "ROAD_FREIGHT":
                ln["unit_cost_cents"] = int(round(ln["unit_cost_cents"] * zfac))
                ln["note"] = (ln.get("note") or "") + f", delivery/collection zone {zone_km:,.0f} km".replace(",", " ")
        base = sum(ln["unit_cost_cents"] * ln["qty"] for ln in ml if ln["code"] == "ROAD_FREIGHT")
        for ln, s in [(ln, s) for ln in ml for s in card.get("surcharges") or [] if ln["code"] == s["code"] and s.get("basis") == "PERCENT"]:
            ln["unit_cost_cents"] = int(round(base * float(s["pct"]) / 100))
    lines += ml
    crossing_coverage = ferry_coverage(card, ml) if card["mode"] == "road" else None
    main_factor = carrier.get("co2_factor_g_tkm") or _default_factor(card["service"], km_main)
    transit = int(card.get("transit_days") or 0)
    if card.get("km_per_day"):
        transit = max(1, math.ceil(driven_km / float(card["km_per_day"]))) + transit
    if zone_km > 250:
        transit += 1
    legs.append({"seq": seq, "kind": "main", "role": "main", "mode": card["mode"], "from": _leg_loc(main_from), "to": _leg_loc(main_to),
                 "carrier_id": card["carrier_id"], "carrier_name": carrier["name"], "transit_days": transit,
                 "distance_km": round(km_main), "service": card["service"], "rate_card_id": card["id"], "via": card.get("via") or [],
                 "segments": segs, "_co2_parts": _co2_parts(segs, main_factor, card["mode"]),
                 "ferry_coverage": crossing_coverage,
                 "_valid_to": card.get("valid_to"), "_weekdays": card.get("departure_weekdays")})
    seq += 1
    final_loc = main_to

    # --- connection (feeder / rail) ---------------------------------------------------------------------
    if c2:
        car2 = carriers[c2["carrier_id"]]
        c2_loc = endpoint_loc(c2["destination"], c2.get("mode"))
        segs2 = routed_segments(c2["mode"], [d_loc] + _via_points(c2) + [c2_loc])
        km2 = sum(s["km"] for s in segs2)
        code2 = "FEEDER_FREIGHT" if c2["mode"] == "sea" else "RAIL_FREIGHT"
        l2 = price_card(c2, m, km=km2, kind="connection", leg_seq=seq, freight_code=code2)
        if not l2:
            return None
        lines += l2
        f2 = car2.get("co2_factor_g_tkm") or _default_factor("FEEDER" if c2["mode"] == "sea" else c2["service"], km2)
        legs.append({"seq": seq, "kind": "connection", "role": "main", "mode": c2["mode"], "from": _leg_loc(d_loc), "to": _leg_loc(c2_loc),
                     "carrier_id": c2["carrier_id"], "carrier_name": car2["name"], "transit_days": c2.get("transit_days"),
                     "distance_km": round(km2), "service": c2["service"], "rate_card_id": c2["id"], "segments": segs2,
                     "_co2_parts": _co2_parts(segs2, f2, c2["mode"]), "_valid_to": c2.get("valid_to"),
                     "_weekdays": c2.get("departure_weekdays")})
        seq += 1
        final_loc = c2_loc

    # --- on-carriage ----------------------------------------------------------------------------------------
    if d_cov[0] == "haul":
        h = haul_cost(dest["country"], final_loc, dest, haul_family, m)
        if not h:
            return None
        hc, hl, km = h
        hcar = carriers.get(hc["carrier_id"], {})
        en, fi = line_text("DELIVERY_FI" if dest["country"] == "FI" else "DELIVERY")
        for ln in hl:
            ln.update(leg_seq=seq, stage="destination")
            if ln["code"] == "PICKUP":
                ln.update(code="DELIVERY", description=en, description_fi=fi)
        lines += hl
        legs.append({"seq": seq, "kind": "on_carriage", "role": "delivery", "mode": "road", "from": _leg_loc(final_loc), "to": _leg_loc(dest),
                     "carrier_id": hc["carrier_id"], "carrier_name": hcar.get("name"), "transit_days": 1 if km <= 450 else 2,
                     "distance_km": round(km), "service": hc["service"], "rate_card_id": hc["id"],
                     "_co2_parts": [(km, hcar.get("co2_factor_g_tkm") or net.DEFAULT_CO2_G_TKM["HAULAGE"])],
                     "_valid_to": hc.get("valid_to")})

    opt = {
        "mode": card["mode"], "service": card["service"],
        "service_name": card.get("service_name") or SERVICE_NAME.get(card["service"], card["service"]),
        "carrier_id": card["carrier_id"], "carrier_name": carrier["name"], "carrier_code": carrier.get("code"),
        "rate_card_id": card["id"], "legs": legs, "via": _via(card, c2),
        "ferry_coverage": crossing_coverage,
        "rate_source": card.get("source") or "indicative", "on_time_pct": carrier.get("on_time_pct"),
        "_cargo_metrics": m, "_lines": lines, "_partial": partial, "_carrier": carrier, "_card": card, "_c2": c2,
        "_dedupe": "|".join([card["carrier_id"], card["service"], c2["carrier_id"] if c2 else "", str(bool(m["is_fcl"])),
                             str(c2["origin"].get("unlocode") if c2 else ""),
                             str((card["destination"].get("unlocode") or "") if card["mode"] == "rail" else "")]),
    }
    if m.get("containers") and not req["cargo"].get("containers"):
        opt["suggested_containers"] = m["containers"]
    return await _finish_option(req, opt, settings, company)


def haversine(a, b):
    return net.haversine_km(a, b)


def _leg_loc(loc: dict | None) -> dict | None:
    if not loc:
        return None
    return {k: loc.get(k) for k in ("unlocode", "name", "city", "country", "lat", "lon")}


def _via(card, c2) -> list[str]:
    via = list(card.get("via") or [])
    if c2 and card["destination"].get("unlocode") and card["destination"]["unlocode"] not in via:
        via.append(card["destination"]["unlocode"])
    return via


def _default_factor(service: str, km: float) -> float:
    if service == "AIR":
        return net.DEFAULT_CO2_G_TKM["AIR_SHORT" if km < 1500 else "AIR"]
    return net.DEFAULT_CO2_G_TKM.get(service, 60.0)


# ------------------------------------------------------------------------------------------------
# Option completion: fees, customs, insurance, VAT, payer, schedule, CO2, liability
# ------------------------------------------------------------------------------------------------
def _sell(lines: list[dict], markup_pct: float):
    for ln in lines:
        q = ln["qty"]
        if ln.get("is_pass_through"):
            ln["unit_price_cents"] = ln["unit_cost_cents"]
        elif ln.get("fixed_price"):
            pass
        else:
            ln["unit_price_cents"] = int(round(ln["unit_cost_cents"] * (1 + markup_pct / 100.0)))
        ln["amount_cents"] = int(round(ln["unit_price_cents"] * q))
        ln["cost_cents"] = int(round(ln["unit_cost_cents"] * q))


def _fee_line(code: str, stage: str, amount_eur: float, qty: float = 1, note: str | None = None, basis="PER_SHIPMENT") -> dict:
    en, fi = line_text(code)
    cents = int(round(amount_eur * 100))
    return {"code": code, "description": en, "description_fi": fi, "stage": stage, "basis": basis, "qty": qty,
            "unit_cost_cents": 0, "unit_price_cents": cents, "fixed_price": True, "is_pass_through": False,
            "estimated": False, "note": note, "leg_seq": None}


def _pass_line(code: str, amount_cents: int, note: str | None = None) -> dict:
    en, fi = line_text(code)
    return {"code": code, "description": en, "description_fi": fi, "stage": "pass_through", "basis": "ACTUAL", "qty": 1,
            "unit_cost_cents": int(amount_cents), "unit_price_cents": int(amount_cents), "amount_cents": int(amount_cents),
            "cost_cents": int(amount_cents), "is_pass_through": True, "estimated": True, "note": note, "leg_seq": None,
            "vat_code": "OUT_OF_SCOPE_DISBURSEMENT"}


def _vat_code_for(line: dict, req: dict, leg_by_seq: dict) -> str:
    if line.get("vat_code"):
        return line["vat_code"]
    if line.get("is_pass_through"):
        return "OUT_OF_SCOPE_DISBURSEMENT"
    direction = req["direction"]
    if direction == "domestic":
        return "FI_25_5"
    if line["code"] in ("CUSTOMS", "CUSTOMS_ITEMS", "EXPORT_CUSTOMS", "ICS2"):
        return CUSTOMS_FEE_VAT_CODE if direction == "import" else "ZERO_EXPORT" if direction == "export" else CUSTOMS_FEE_VAT_CODE
    leg = leg_by_seq.get(line.get("leg_seq"))
    if leg and leg["kind"] in ("pre_carriage", "on_carriage") and leg["from"]["country"] == "FI" and leg["to"]["country"] == "FI":
        return DOMESTIC_LEG_VAT_CODE
    return "ZERO_INTL_TRANSPORT"


def _liability(req: dict, opt: dict) -> str:
    # Domestic options use the forwarding terms, irrespective of the proposed mode.
    if req["origin"]["country"] == req["destination"]["country"]:
        return "NSAB"
    mode = opt["mode"]
    if mode == "road":
        return "CMR" if req["origin"]["country"] != req["destination"]["country"] else "NSAB"
    return {"sea": "HAGUE_VISBY", "air": "MONTREAL", "rail": "CIM"}.get(mode, "NSAB")


_NOTE_FI = [
    (re.compile(r"(\d) ?kg chargeable \(1:6000\)"), r"\1 kg rahdituspaino (1:6000)"),
    (re.compile(r"kg chargeable \(333 kg/m³, 1 850 kg/ldm\)"), "kg rahdituspaino (333 kg/m³, 1 850 kg/lvm)"),
    (re.compile(r"W/M \(1 m³ = 1 000 kg, min 1\)"), "W/M (1 m³ = 1 000 kg, väh. 1)"),
    (re.compile(r"× full trailer \(13\.6 ldm\)"), "× täysi perävaunu (13,6 lvm)"),
    (re.compile(r"× 13\.6 m trailer"), "× 13,6 m perävaunu"),
    (re.compile(r"minimum charge|\bminimum\b"), "minimiveloitus"),
    (re.compile(r"% of freight"), "% rahdista"),
    (re.compile(r"delivery/collection zone"), "jakelu-/noutovyöhyke"),
    (re.compile(r"DG acceptance and handling"), "VAK-hyväksyntä ja -käsittely"),
    (re.compile(r"items beyond (\d+)"), r"\1 nimikkeen ylittävät"),
    (re.compile(r"insured value"), "vakuutusarvo"),
    (re.compile(r"EUR 3 per item category, consignments up to EUR 150 \(from 1 Jul 2026\)"),
     "3 EUR tavaralajilta, lähetykset enintään 150 EUR (1.7.2026 alkaen)"),
    (re.compile(r"% of customs value (.+?) EUR \(your rate; final at classification\)"),
     r"% tullausarvosta \1 EUR (antamasi tulliprosentti; lopullinen luokittelussa)"),
    (re.compile(r"% of (.+?) EUR, payable to Customs"), r"% summasta \1 EUR, maksetaan Tullille"),
    # s5 benchmark estimates (services/global_quote.py)
    (re.compile(r"groupage share of a truck"), "kappaletavaran osuus kuorma-autosta"),
    (re.compile(r"× container truck"), "× konttiauto"),
    (re.compile(r"customs border, transit documents"), "tulliraja, passitusasiakirjat"),
    (re.compile(r"index all-in: base freight incl\. BAF/CAF/PSS"), "indeksihinta: perusrahti sis. BAF, CAF ja PSS"),
    (re.compile(r"index all-in incl\. fuel/security"), "indeksihinta sis. polttoaine- ja turvalisät"),
    (re.compile(r", feeder port"), ", syöttösatama"),
    (re.compile(r"small-shipment break × "), "pienlähetyskerroin × "),
    (re.compile(r"(\d) ?kg chargeable \(1:5000\)"), r"\1 kg rahdituspaino (1:5000)"),
    (re.compile(r"Finnish export tariff of the destination zone"), "kohdevyöhykkeen vientitariffi Suomesta"),
    (re.compile(r"express model EUR (\S+) \+ (\S+)/kg"), r"pikalähetysmalli \1 EUR + \2 EUR/kg"),
    (re.compile(r" notice \((\d{4}-\d{2}-\d{2})\)"), r" -ilmoitus (\1)"),
    (re.compile(r"1/(\d+) of the 40' (?:charge|level)"), r"1/\1 40 jalan kontin hinnasta"),
    (re.compile(r"intermodal rail (.+?) km incl\. lifts"), r"intermodaalirautatie \1 km sis. nostot"),
    (re.compile(r"partner rate"), "kumppanin hinta"),
]


REASON_FI = {
    "incoterm.seller": "myyjä maksaa ({inc})", "incoterm.buyer": "ostaja maksaa ({inc})",
    "duty.unclassified": "riippuu tullinimikkeestä; tarkentuu luokittelussa",
    "duty.value_missing": "anna tavaran arvo nähdäksesi arvion",
    "vat.postponed": "tilitysmenettely: ilmoitetaan ja vähennetään oman arvonlisäveroilmoituksen kautta",
    "vat.depends": "tilitysmenettelyssä arvonlisäveroilmoituksen kautta, muuten maksetaan Tullille",
    "pickup.on_request": "hinnoitellaan erikseen (paikallista tariffia ei ole ladattu)",
}


def reason_fi(code: str | None, reason: str | None, inc: str | None = None) -> str | None:
    if code in REASON_FI:
        return REASON_FI[code].format(inc=inc or "")
    return reason


def note_fi(note: str | None) -> str | None:
    """Finnish version of the generated line notes (numbers keep their values; separators follow Finnish style)."""
    if not note:
        return note
    out = note
    for rx, rep in _NOTE_FI:
        out = rx.sub(rep, out)
    out = re.sub(r"(\d),(\d{3})", r"\1 \2", out)
    out = re.sub(r"(\d)\.(\d)", r"\1,\2", out)
    return out


async def _finish_option(req: dict, opt: dict, settings: dict, company: dict | None) -> dict:
    lines: list[dict] = opt.pop("_lines")
    m = opt.pop("_cargo_metrics")
    carrier = opt.pop("_carrier")
    card = opt.pop("_card")
    opt.pop("_c2", None)
    cargo = req["cargo"]
    direction = req["direction"]
    o_eu = req["origin"]["country"] in net.EU_COUNTRIES
    d_eu = req["destination"]["country"] in net.EU_COUNTRIES
    warnings: list[dict] = []
    not_included: list[dict] = list(opt.pop("_partial"))

    # Public tariffs are opt-in on an actual carrier card; they never establish a freight offer.
    if card.get("public_surcharge_refs"):
        from services.public_surcharges import quote_lines
        surcharge_lines, surcharge_errors = await quote_lines(req, opt, card, carrier, m, lines)
        lines.extend(surcharge_lines)
        if surcharge_errors:
            opt["_public_surcharge_errors"] = surcharge_errors
            opt["_not_quoted"] = {"mode": opt["mode"], "service": opt.get("service"),
                                  "carrier_id": opt.get("carrier_id"), "code": "public_tariff.unavailable",
                                  "rate_card_id": opt.get("rate_card_id"),
                                  "reason": "Required public surcharge could not be calculated", "details": surcharge_errors}
            return opt

    # Overseas agents (s3): origin/destination handling by the agent under a signed agreement, from its rate card
    for role, country in (("origin", req["origin"]["country"]), ("destination", req["destination"]["country"])):
        if country == HOME_COUNTRY:
            continue
        ac = await agents.agent_charges(country, role, opt["mode"], m, on_date=date.fromisoformat(req["ready_date"]),
                                        company_id=(company or {}).get("id"))
        if ac:
            lines.extend(dict(ln) for ln in ac["lines"])
            opt.setdefault("agents", []).append({k: ac[k] for k in ("agent_id", "agent_name", "role", "country", "rate_card_id", "agreement_id")})

    # DG surcharge (RULE D-5)
    if cargo.get("dangerous_goods") and not any(ln["code"] == "DG" for ln in lines):
        default = {"FCL": 150, "RAIL_FCL": 120, "LCL": 95, "AIR": 85, "ROAD_LTL": 65, "ROAD_FTL": 90, "RORO": 90}.get(opt["service"], 90)
        per = sum(c["count"] for c in m["containers"]) if opt["service"] in ("FCL", "RAIL_FCL") else 1
        main_seq = next(lg["seq"] for lg in opt["legs"] if lg["kind"] == "main")
        lines.append(_cost_line("DG", "main", "PER_CONTAINER" if per > 1 else "PER_SHIPMENT", per, default * 100,
                                leg_seq=main_seq, note="DG acceptance and handling"))

    # Forwarder fees
    if direction != "domestic":
        lines.append(_fee_line("DOC_FEE", "fees", settings["documentation_fee_eur"]))
    # Filing is postponed until representation is arranged; no fictitious filing fee or registration.
    formalities = customs_border.postponed_row(req)
    if formalities:
        not_included.append(formalities)

    rule = await margin_rules.resolve_margin(req, opt, company, settings)
    markup = rule["margin_pct"]
    _sell(lines, markup)

    # Insurance (RULE X-1): 110 % of CIF value (goods + freight), premium rate x cover factor, minimum premium.
    cover = (req.get("insurance") or {}).get("cover", "NONE")
    if cover != "NONE":
        if cargo.get("value_cents"):
            freight = sum(ln["amount_cents"] for ln in lines if ln["stage"] in ("origin", "main"))
            insured = int(round((cargo["value_cents"] + freight) * 1.10))
            factor = {"ICC_A": 1.0, "ICC_B": 0.8, "ICC_C": 0.6}[cover]
            premium = max(int(round(insured * settings["insurance_rate_pct"] / 100 * factor)),
                          int(round(settings["insurance_min_premium_eur"] * 100)))
            ln = _pass_line("INSURANCE", premium, note=f"{cover.replace('_', ' (')}), insured value {insured / 100:,.0f} EUR (110 %)")
            ln.update(stage="insurance", estimated=False, basis="PERCENT", insured_value_cents=insured)
            ln["description"] = f"Cargo insurance {cover.replace('ICC_', 'ICC (')})"
            ln["description_fi"] = f"Kuljetusvakuutus {cover.replace('ICC_', 'ICC (')})"
            lines.append(ln)
        else:
            warnings.append(_warn("insurance.value_missing", "Insurance needs the cargo value; it is not included yet."))

    # Explicit requests for our import service retain informational duty/VAT estimates.
    # An estimate is not a filing; the filing row remains postponed above.
    if req.get("customs_clearance") and req["customs_direction"] == "import":
        await _duty_and_vat(req, opt, lines, not_included, settings, company)

    for ln in lines:
        ln["note_fi"] = note_fi(ln.get("note"))

    # VAT codes, payer and incoterm split (RULE V-1, Q-3)
    leg_by_seq = {lg["seq"]: lg for lg in opt["legs"]}
    inc = req.get("incoterm")
    seller_stages = SELLER_STAGES.get(inc, None)
    role = req["customer_role"]
    charged = []
    for ln in lines:
        ln["vat_code"] = _vat_code_for(ln, req, leg_by_seq)
        ln["vat_rate_pct"] = VAT_RATES.get(ln["vat_code"], 0.0) if ln["vat_code"] != "FI_25_5" else float(settings.get("vat_rate_standard_pct", 25.5))
        ln.pop("fixed_price", None)
        stage = ln["stage"]
        if seller_stages is None or stage in ("fees",):
            ln["payer"] = "CONSIGNEE" if role == "buyer" else "SHIPPER"
            charged.append(ln)
            continue
        paid_by_seller = stage in seller_stages
        ln["payer"] = "SHIPPER" if paid_by_seller else "CONSIGNEE"
        if stage == "insurance":  # the customer asked for cover: charge it to them, warn under C-terms (X-2)
            charged.append(ln)
            continue
        if (role == "seller") == paid_by_seller:
            charged.append(ln)
        else:
            other = "seller" if paid_by_seller else "buyer"
            not_included.append({"code": ln["code"], "description": ln["description"], "description_fi": ln.get("description_fi"),
                                 "amount_cents": ln["amount_cents"], "reason": f"Paid by the {other} under {inc}",
                                 "reason_code": f"incoterm.{other}"})
    if inc and not any(ln["stage"] in ("main",) for ln in charged):
        warnings.append(_warn("incoterm.other_party_main", f"Under {inc} the main carriage is paid by the other party; "
                                                           f"this quote covers only your part.", "info", incoterm=inc))

    floor = margin_rules.apply_floor(charged, rule["floor_cents"], rule.get("cap_pct"))
    total = sum(ln["amount_cents"] for ln in charged)
    cost = sum(ln["cost_cents"] for ln in charged)
    pass_through = sum(ln["amount_cents"] for ln in charged if ln["is_pass_through"])
    vat = sum(int(round(ln["amount_cents"] * ln["vat_rate_pct"] / 100)) for ln in charged)
    margin = sum(ln["amount_cents"] - ln["cost_cents"] for ln in charged if not ln["is_pass_through"])
    sell_own = total - pass_through
    opt["lines"] = charged
    for n in not_included:
        if n.get("reason_code") in ("customs.customer_broker", "customs.postponed"):
            continue
        n["reason_fi"] = reason_fi(n.get("reason_code"), n.get("reason"), inc)
    opt["not_included"] = not_included
    opt.update(subtotal_cents=sell_own, pass_through_cents=pass_through, total_cents=total, vat_cents=vat,
               total_incl_vat_cents=total + vat, cost_cents=cost, margin_cents=margin, markup_pct=markup,
               margin_pct=round(100 * margin / sell_own, 1) if sell_own else 0.0,
               margin_rule_id=rule["rule_id"], margin_rule_name=rule["rule_name"], margin_source=rule["source"], margin_floor_cents=floor)

    # Schedule
    await _schedule(req, opt, carrier, card)
    # CO2e (GLEC-style default intensities)
    co2 = 0.0
    dist_total = 0.0
    for lg in opt["legs"]:
        parts = lg.pop("_co2_parts", None) or [(lg["distance_km"], 60.0)]
        leg_co2 = sum(km * m["co2_weight_t"] * f / 1000.0 for km, f in parts)
        co2 += leg_co2
        dist_total += lg["distance_km"]
        lg["co2e_kg"] = round(leg_co2, 1)
    opt["co2e_kg"] = round(co2, 1)
    opt["co2"] = {"method": "GLEC Framework default intensities (well-to-wheel), indicative",
                  "distance_km": round(dist_total), "weight_t": round(m["co2_weight_t"], 3),
                  "weight_assumed": m.get("co2_weight_assumed", False)}
    regime = _liability(req, opt)
    opt["liability_regime"] = regime
    opt["liability"] = {"regime": regime, **LIABILITY[regime],
                        "forwarder": "NSAB 2015 (PSYM 2015) §§ 22-23: forwarder as contracting party, network principle applies"}
    opt["forwarder_role"] = "principal"
    opt["chargeable"] = _chargeable_summary(opt["service"], m)
    valid_to = [lg.pop("_valid_to", None) for lg in opt["legs"]]
    for lg in opt["legs"]:
        lg.pop("_weekdays", None)
    cap = (now() + timedelta(days=int(settings["quote_validity_days"]))).date()
    vts = [date.fromisoformat(v[:10]) for v in valid_to if v] + [cap]
    opt["valid_until"] = min(vts).isoformat()
    opt["warnings"] = warnings
    opt["dg_accepted"] = True
    opt["id"] = "opt_" + hashlib.sha1((opt["_dedupe"] + "|" + str(opt["total_cents"])).encode()).hexdigest()[:10]
    opt["recommended"] = False
    opt["rank_reason"] = None
    opt["tags"] = []
    if not_included and any(n.get("code") == "PICKUP" and n.get("amount_cents") is None for n in not_included):
        opt["warnings"].append(_warn("option.partial_origin", "Pickup at origin is priced on request.", "info"))
    return opt


def _chargeable_summary(service: str, m: dict) -> dict:
    ch = chargeable(service if service in ("AIR", "LCL", "ROAD_FTL", "RORO") else
                    ("HAULAGE_LTL" if service == "ROAD_LTL" else "FCL"), m)
    return {"basis": ch.get("basis"), "label": ch.get("label"), "weight_kg": m.get("weight_kg"), "volume_m3": m.get("volume_m3"),
            "ldm": m.get("ldm"), "chargeable_kg": ch.get("chargeable_kg"), "wm": ch.get("wm"), "containers": m.get("containers") or []}


async def _schedule(req: dict, opt: dict, carrier: dict, card: dict):
    ready = date.fromisoformat(req["ready_date"])
    t = ready
    etd = None
    live_used = False
    for lg in opt["legs"]:
        if lg["kind"] == "pre_carriage":
            lg["etd"] = t.isoformat()
            t = t + timedelta(days=lg["transit_days"] or 1)
            lg["eta"] = t.isoformat()
            continue
        if lg["kind"] in ("main", "connection"):
            cutoff = 1 if lg["mode"] in ("sea", "rail") and lg["kind"] == "main" else 0
            dep = _next_departure(t + timedelta(days=cutoff if lg["kind"] == "main" else 1), lg.get("_weekdays"))
            arr = dep + timedelta(days=int(lg["transit_days"] or 1))
            if lg["kind"] == "main" and lg["mode"] == "sea":
                live = await net.live_schedule(carrier, lg["from"] or {}, lg["to"] or {}, dep)
                if live:
                    dep, arr = date.fromisoformat(live["etd"]), date.fromisoformat(live["eta"])
                    live_used = True
            lg["etd"], lg["eta"] = dep.isoformat(), arr.isoformat()
            etd = etd or dep
            t = arr
            continue
        lg["etd"] = t.isoformat()
        t = t + timedelta(days=lg["transit_days"] or 1)
        lg["eta"] = t.isoformat()
    etd = etd or ready
    opt["etd"], opt["eta"] = etd.isoformat(), t.isoformat()
    opt["transit_days"] = (t - etd).days
    opt["door_to_door_days"] = (t - ready).days
    opt["schedule_source"] = "carrier_api" if live_used else "rate_card"


async def _duty_and_vat(req, opt, lines, not_included, settings, company):
    cargo = req["cargo"]
    value = cargo.get("value_cents")
    if not value:
        not_included.append({"code": "DUTY", "description": "Customs duty and import VAT", "description_fi": "Tulli ja tuonnin arvonlisävero", "amount_cents": None,
                             "reason": "Give the cargo value to see an estimate", "reason_code": "duty.value_missing"})
        return
    inc = req.get("incoterm")
    # RULE I-5: customs value = transaction value + freight/insurance to the EU border.
    freight_to_border = 0
    if inc in (None, "EXW", "FCA", "FAS", "FOB"):
        main_seq = next((lg["seq"] for lg in opt["legs"] if lg["kind"] == "main"), None)
        freight_to_border = sum(ln["amount_cents"] for ln in lines
                                if ln["stage"] == "origin" or (ln["stage"] == "main" and ln.get("leg_seq") == main_seq))
    main_seq = next((lg["seq"] for lg in opt["legs"] if lg["kind"] == "main"), None)
    customs_value = value + freight_to_border
    duty = None
    note = None
    # The EUR 150 low-value threshold uses the intrinsic value of the goods (transport and insurance excluded).
    if value <= 15000 and now().date() >= LOW_VALUE_DUTY_FROM:
        duty = 300 * max(1, cargo.get("customs_items") or 1)
        note = "EUR 3 per item category, consignments up to EUR 150 (from 1 Jul 2026)"
    elif cargo.get("duty_rate_pct") is not None:
        duty = int(round(customs_value * cargo["duty_rate_pct"] / 100))
        note = f"{cargo['duty_rate_pct']:g} % of customs value {customs_value / 100:,.0f} EUR (your rate; final at classification)"
    if duty is not None:
        lines.append(_pass_line("DUTY", duty, note=note))
    else:
        not_included.append({"code": "DUTY", "description": "Customs duty", "description_fi": "Tulli", "amount_cents": None,
                             "reason": "Depends on the CN code; set when the goods are classified", "reason_code": "duty.unclassified"})
    # Import VAT base (ALV 85 §): customs value + duty + transport after the border to the first destination in Finland.
    post_border = sum(ln["amount_cents"] for ln in lines
                      if ln["stage"] == "destination" or (ln["stage"] == "main" and ln.get("leg_seq") not in (None, main_seq)))
    vat_rate = cargo.get("vat_rate_pct") or float(settings.get("vat_rate_standard_pct", 25.5))
    vat_base = customs_value + (duty or 0) + post_border
    import_vat = int(round(vat_base * vat_rate / 100))
    registered = (company or {}).get("vat_registered")
    if registered:
        not_included.append({"code": "IMPORT_VAT", "description": "Import VAT", "description_fi": "Tuonnin arvonlisävero",
                             "amount_cents": import_vat, "reason": "Postponed: declared and deducted in your VAT return (tilitysmenettely)",
                             "reason_code": "vat.postponed"})
    elif company is None:
        not_included.append({"code": "IMPORT_VAT", "description": "Import VAT", "description_fi": "Tuonnin arvonlisävero",
                             "amount_cents": import_vat, "reason": "Postponed via the VAT return if you are VAT-registered in Finland; otherwise payable to Customs",
                             "reason_code": "vat.depends"})
    else:
        ln = _pass_line("IMPORT_VAT", import_vat, note=f"{vat_rate:g} % of {vat_base / 100:,.0f} EUR, payable to Customs")
        lines.append(ln)


# ------------------------------------------------------------------------------------------------
# Ranking
# ------------------------------------------------------------------------------------------------
def rank(options: list[dict], limit: int = 10) -> list[dict]:
    if not options:
        return []
    min_price = min(o["total_cents"] for o in options) or 1
    min_days = min(o["door_to_door_days"] for o in options) or 1
    min_co2 = min(o["co2e_kg"] for o in options) or 0.1

    def score(o):
        rel = (o.get("on_time_pct") or 70) / 100.0
        alloc = o.get("allocation") or {}
        # allocations (s3): a carrier behind its MQC saves the shortfall penalty; a carrier at its cap cannot take it
        effective = o["total_cents"] - (alloc.get("ranking_credit_cents") or 0)
        return (effective / min_price + 0.35 * o["door_to_door_days"] / min_days
                + 0.12 * (o["co2e_kg"] / min_co2 if min_co2 else 1) ** 0.5 + 0.25 * (1 - rel) + (1.0 if alloc.get("full") else 0.0))

    for o in options:
        o["_score"] = round(score(o), 4)
        o["tags"] = ["allocation_full"] if (o.get("allocation") or {}).get("full") else []
    cheapest = min(options, key=lambda o: (o["total_cents"], o["door_to_door_days"]))
    fastest = min(options, key=lambda o: (o["door_to_door_days"], o["total_cents"]))
    greenest = min(options, key=lambda o: (o["co2e_kg"], o["total_cents"]))
    best = min(options, key=lambda o: o["_score"])
    cheapest["tags"].append("cheapest")
    fastest["tags"].append("fastest")
    greenest["tags"].append("lowest_co2")
    best["tags"].append("best_value")
    for o in options:
        o["recommended"] = o is best
        for tag in ("best_value", "cheapest", "fastest", "lowest_co2"):
            if tag in o["tags"]:
                o["rank_reason"] = tag if o is not best else "best_value"
                break
    ordered = sorted(options, key=lambda o: o["_score"])
    keep, per_service = [], {}
    for o in ordered:
        k = (o["mode"], o["service"])
        if o["tags"] or per_service.get(k, 0) < 5:
            keep.append(o)
            per_service[k] = per_service.get(k, 0) + 1
    keep = keep[:limit]
    for o in keep:
        o.pop("_score", None)
    return keep


# ------------------------------------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------------------------------------
async def quote_pricing(request: dict, *, company: dict | None = None, strict: bool = False) -> dict:
    settings = await get_settings()
    req = await normalize_request(request, strict=strict)
    warnings = req.pop("_warnings")
    m = cargo_metrics(req["cargo"])
    if req["cargo"]["load_type"] != "FCL":
        if not req["cargo"].get("volume_m3"):
            warnings.append(_warn("cargo.volume_missing", "No volume given: air and groupage prices use weight and pallet "
                                                          "footprint only; the final price uses measured dimensions.", "info"))
    if req["direction"] == "import" and req.get("incoterm") == "DDP" and req["customer_role"] == "buyer":
        warnings.append(_warn("incoterm.ddp_import", "Under DDP the seller clears import and pays duty and VAT; the seller "
                                                     "needs a Finnish VAT number or fiscal representative (RULE I-3).", "info"))
    options, not_quoted, w2 = await build_options(req, m, settings, company)
    warnings += w2
    for o in options:  # every rate-table option is a firm price (seeded cards say "indicative" separately)
        o.setdefault("firmness", "firm")
        o.setdefault("bookable", True)
        o.setdefault("price_basis", "spot")
        for lg in o.get("legs") or []:
            lg.setdefault("firm", True)
            lg.setdefault("pricing", {"kind": "partner_rate" if o.get("rate_source") == "partner_network" else "rate_card",
                                      "rate_card_id": lg.get("rate_card_id")})
    sourcing_needs: list[dict] = []
    if settings.get("global_quoting_enabled", True):
        from services import global_quote

        extra, w3 = await global_quote.extend_options(req, m, settings, company, options, not_quoted=not_quoted)
        options += extra
        warnings += w3
    if req["cargo"].get("dangerous_goods") or req["cargo"].get("dg_items"):  # s12: UN dataset, refuses forbidden modes
        from services import dangerous_goods
        try:
            options = await dangerous_goods.apply_to_quote(req, options, warnings, not_quoted)
        except ValueError as exc:
            raise QuoteInputError(str(exc)) from None
    await allocations.annotate_options(options, req, m, company_id=(company or {}).get("id"))
    await scorecards.annotate_options(options)
    options = rank(options, limit=12)
    if settings.get("global_quoting_enabled", True):
        options = global_quote.prefer_firm(options)
        sourcing_needs = global_quote.sourcing_needs(options)
    for o in options:
        o.pop("_sourcing", None)
    if req["cargo"].get("dangerous_goods"):
        warnings.append(_warn("dg.approval", "Dangerous goods: our team checks the DG details and carrier acceptance "
                                             "before the quote is confirmed.", "info"))
    if not options:
        warnings.append(_warn("lane.out_of_network", "No rates loaded for this lane yet. Our pricing team will quote it "
                                                     "manually, usually within one working day.", "warning"))
    elif not any(o.get("firmness") == "firm" for o in options):
        warnings.append(_warn("lane.estimate_only", "No carrier rate on file for this route yet: the prices are estimates "
                                                    "from dated benchmark indices until partner rates arrive.", "info"))
    if any(o.get("rate_source") == "indicative" for o in options):
        warnings.append(_warn("rates.indicative", "Indicative 2026 market rates, not a carrier contract; the booked price "
                                                  "is confirmed by our team.", "info"))
    valid_until = min((o["valid_until"] for o in options), default=(now() + timedelta(days=int(settings["quote_validity_days"]))).date().isoformat())
    req["cargo_summary"] = cargo_summary(req["cargo"])
    priced = {"request": req, "options": options, "not_quoted": not_quoted, "warnings": warnings,
            "valid_until": valid_until, "sourcing_needs": sourcing_needs, "settings": {"markup_pct": float((company or {}).get("margin_pct") or settings["default_margin_pct"]),
                                                     "terms_version": settings["terms_version"],
                                                     "threshold_eur": settings["quote_approval_threshold_eur"]}}
    priced = await contract_tariffs.apply_contract(priced, company)
    label_indicative(priced.get("options") or [])
    return priced


def label_indicative(options: list[dict]) -> list[dict]:
    """An option priced from an indicative market rate card (no carrier contract) is labelled price_status "indicative":
    "Indicative price, confirmed at booking". A label only: firmness, bookable and the approval gates are unchanged."""
    for o in options:
        if o.get("rate_source") == "indicative" and o.get("price_basis") != "contract":
            o["price_status"] = "indicative"
    return options


async def price_request(request: dict, *, company: dict | None = None) -> list[dict]:
    """Contract function (ARCHITECTURE.md): priced options for a quote request."""
    return (await quote_pricing(request, company=company))["options"]


def recompute_option(option: dict, *, markup_pct: float | None = None, vat_standard: float = 25.5) -> dict:
    """Recalculate amounts and totals after ops edited lines or the markup (PATCH /quotes/{id})."""
    for ln in option.get("lines") or []:
        ln["qty"] = float(ln.get("qty") or 0)
        ln["unit_cost_cents"] = int(ln.get("unit_cost_cents") or 0)
        if ln.get("vat_code") not in VAT_CODES:
            raise QuoteInputError(f"Unknown VAT code {ln.get('vat_code')!r} on line {ln.get('description') or ln.get('code')}.")
        if markup_pct is not None and not ln.get("is_pass_through") and ln["unit_cost_cents"] > 0:
            ln["unit_price_cents"] = int(round(ln["unit_cost_cents"] * (1 + markup_pct / 100.0)))
        ln["unit_price_cents"] = int(ln.get("unit_price_cents") if ln.get("unit_price_cents") is not None else ln["unit_cost_cents"])
        if ln.get("is_pass_through"):
            ln["unit_price_cents"] = ln["unit_cost_cents"] or ln["unit_price_cents"]
            ln["unit_cost_cents"] = ln["unit_price_cents"]
        ln["amount_cents"] = int(round(ln["unit_price_cents"] * ln["qty"]))
        ln["cost_cents"] = int(round(ln["unit_cost_cents"] * ln["qty"]))
        ln["vat_rate_pct"] = vat_standard if ln["vat_code"] == "FI_25_5" else VAT_RATES[ln["vat_code"]]
        ln.setdefault("stage", "main")
        ln.setdefault("payer", "CONSIGNEE")
        ln.setdefault("description_fi", ln.get("description"))
        ln["note_fi"] = ln.get("note_fi") or note_fi(ln.get("note"))
    lines = option.get("lines") or []
    total = sum(ln["amount_cents"] for ln in lines)
    pass_through = sum(ln["amount_cents"] for ln in lines if ln.get("is_pass_through"))
    margin = sum(ln["amount_cents"] - ln["cost_cents"] for ln in lines if not ln.get("is_pass_through"))
    vat = sum(int(round(ln["amount_cents"] * ln["vat_rate_pct"] / 100)) for ln in lines)
    own = total - pass_through
    if markup_pct is not None:
        option["markup_pct"] = markup_pct
    option.update(total_cents=total, pass_through_cents=pass_through, subtotal_cents=own, vat_cents=vat,
                  total_incl_vat_cents=total + vat, cost_cents=sum(ln["cost_cents"] for ln in lines), margin_cents=margin,
                  margin_pct=round(100 * margin / own, 1) if own else 0.0)
    return option


def today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()
