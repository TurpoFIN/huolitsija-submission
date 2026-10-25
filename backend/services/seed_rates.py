"""Demo carrier network and indicative 2026 rate cards (startup order 20, SEED_DEMO only, demo: true).

Levels are indicative 2026 market levels for buy rates (EUR), set by hand from public market commentary
and typical tariff structures (ocean through-rates incl. feeder to Finland, LCL per W/M, air per chargeable kg
with weight breaks, road groupage per chargeable kg with fuel floaters, ro-ro per lane metre, domestic tariffs by
weight and distance band). They are NOT carrier contracts: every card has source "indicative" and the UI says so.
Ops replace them with contract or spot rates in /ops/rates. Validity windows are relative to the seed date so a
fresh environment always has valid, expiring, expired and upcoming cards.
"""
from __future__ import annotations

import calendar
import uuid
from datetime import date, timedelta

from core import config
from core.db import db, register_index
from core.lifecycle import on_startup
from core.util import now, now_iso

register_index("carriers", "id", unique=True)
register_index("carriers", "code")
register_index("rate_cards", "id", unique=True)
register_index("rate_cards", "carrier_id")
register_index("rate_cards", [("mode", 1), ("valid_to", 1)])

SEED_KEY = "seed:v2:v3"  # bump when cards are added; upserts are $setOnInsert so re-running is safe


def stable_id(kind: str, key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"huolitsija-demo-{kind}-{key}"))


def carrier_id(code: str) -> str:
    return stable_id("carrier", code)


def C(eur: float) -> int:
    return int(round(eur * 100))


OCEAN_DG = ["1.4", "2.1", "2.2", "2.3", "3", "4.1", "4.2", "4.3", "5.1", "5.2", "6.1", "8", "9"]
FERRY_DG = ["2.1", "2.2", "3", "4.1", "5.1", "6.1", "8", "9"]
ROAD_DG = ["2.1", "2.2", "3", "4.1", "4.2", "4.3", "5.1", "5.2", "6.1", "8", "9"]
AIR_PAX_DG = ["2.2", "3", "4.1", "5.1", "6.1", "8", "9"]
AIR_CAO_DG = ["1.4", "2.1", "2.2", "3", "4.1", "4.2", "4.3", "5.1", "5.2", "6.1", "8", "9"]
RAIL_DG = ["2.1", "2.2", "3", "4.1", "5.1", "6.1", "8", "9"]

# code, name, modes, country, integration type, website, booking e-mail, on-time benchmark %, CO2 g/tkm (None = GLEC default by service),
# DG classes, blocked UN numbers, DG note, services note
CARRIERS = [
    ("MAERSK", "Maersk", ["sea"], "DK", "maersk", "https://www.maersk.com", None, 84, 12.0, OCEAN_DG, [], None,
     "Asia–North Europe via Bremerhaven with feeder to Finland; FI–US East/Gulf via Bremerhaven."),
    ("MSC", "MSC Mediterranean Shipping Company", ["sea"], "CH", "dcsa", "https://www.msc.com", None, 71, 12.0, OCEAN_DG, [], None,
     "Asia–North Europe via Bremerhaven, Gdańsk and Antwerp with feeders to Finnish ports."),
    ("HAPAG", "Hapag-Lloyd", ["sea"], "DE", "dcsa", "https://www.hapag-lloyd.com", None, 85, 12.0, OCEAN_DG, [], None,
     "Asia–North Europe hub-and-spoke via Hamburg; Gothenburg hub; FI–US East Coast."),
    ("CMACGM", "CMA CGM", ["sea"], "FR", "dcsa", "https://www.cma-cgm.com", None, 66, 12.0, OCEAN_DG, [], None,
     "Asia–North Europe via Rotterdam with Containerships feeders to Finland."),
    ("COSCO", "COSCO Shipping Lines", ["sea"], "CN", "partner_portal", "https://lines.coscoshipping.com", None, 64, 12.0, OCEAN_DG, [], None,
     "Asia–North Europe to Hamburg and Gdańsk; port-to-port to the hub or through to Helsinki."),
    ("ONE", "Ocean Network Express", ["sea"], "SG", "dcsa", "https://www.one-line.com", None, 62, 12.0, OCEAN_DG, [], None,
     "Busan, Yantian and Cat Lai to Rotterdam with feeder to Finland."),
    ("UNIFEEDER", "Unifeeder", ["sea"], "DK", "partner_portal", "https://www.unifeeder.com", None, 88, 20.0, OCEAN_DG, [], None,
     "Feeder from Hamburg, Gdańsk and Gothenburg to Helsinki and Kotka."),
    ("CONTAINERSHIPS", "Containerships (CMA CGM Group)", ["sea", "road"], "FI", "partner_portal", "https://www.containershipsgroup.com", None, 87, 20.0, OCEAN_DG, [], None,
     "Short-sea feeder Rotterdam/Hamburg–Finland and container haulage inside Finland."),
    ("FINNLINES", "Finnlines", ["sea"], "FI", "partner_portal", "https://www.finnlines.com", None, 93, 55.0, FERRY_DG, [],
     "Ro-pax: DG on passenger sailings within IMDG 7.1 limits; class 1 and 7 not accepted.",
     "Ro-ro / ro-pax Travemünde–Helsinki and Kapellskär–Naantali, unaccompanied trailers per lane metre."),
    ("TALLINK", "Tallink Grupp", ["sea"], "EE", "partner_portal", "https://www.tallink.com", None, 95, 60.0, FERRY_DG, [],
     "Ro-pax: DG within passenger-ship limits only.", "Tallinn–Helsinki shuttle and Stockholm–Helsinki/Turku freight."),
    ("ECKERO", "Eckerö Line (Finbo Cargo)", ["sea"], "FI", "partner_portal", "https://www.eckeroline.fi", None, 94, 60.0, FERRY_DG, [],
     "Ro-pax: DG within passenger-ship limits only.", "Tallinn–Helsinki ro-pax freight (Finbo Cargo)."),
    ("VIKING", "Viking Line", ["sea"], "FI", "partner_portal", "https://www.vikingline.com", None, 94, 60.0, FERRY_DG, [],
     "Ro-pax: DG within passenger-ship limits only.", "Helsinki–Tallinn and Turku–Stockholm (via Åland) ro-pax freight."),
    ("DFDS", "DFDS", ["sea"], "DK", "partner_portal", "https://www.dfds.com", None, 92, 55.0, FERRY_DG, [], None,
     "Baltic ro-ro Paldiski–Kapellskär for Estonia–Sweden land bridges."),
    ("DSV", "DSV", ["road", "sea"], "DK", "partner_portal", "https://www.dsv.com", None, 91, None, ROAD_DG, [], None,
     "Road groupage and FTL Germany/Benelux/Sweden–Finland via Travemünde; LCL Asia–Finland."),
    ("SCHENKER", "DB Schenker (DSV group)", ["road", "sea"], "DE", "partner_portal", "https://www.dbschenker.com", None, 90, None, ROAD_DG, [], None,
     "Road groupage Germany/Poland–Finland, LCL Asia–Finland. Part of DSV since 2025; Schenker networks still operate under their brand."),
    ("KAUKOKIITO", "Kaukokiito", ["road"], "FI", "partner_portal", "https://www.kaukokiito.fi", None, 95, None, ROAD_DG, [], None,
     "Finnish domestic groupage and FTL; Estonia–Finland via Tallinn."),
    ("POSTI", "Posti Freight", ["road"], "FI", "partner_portal", "https://www.posti.fi", None, 94, None, ROAD_DG, [], None,
     "Finnish domestic groupage and pallets."),
    ("POSTNORD", "PostNord", ["road"], "SE", "partner_portal", "https://www.postnord.com", None, 92, None, ROAD_DG, [], None,
     "Nordic groupage Sweden/Denmark–Finland via Stockholm–Turku."),
    ("GIRTEKA", "Girteka", ["road"], "LT", "manual", "https://www.girteka.eu", None, 89, 62.0, ROAD_DG, [], None,
     "Full truckloads continental Europe–Finland via Via Baltica and Tallinn–Helsinki."),
    ("FINNAIR", "Finnair Cargo", ["air"], "FI", "partner_portal", "https://www.finnaircargo.com", None, 90, None, AIR_PAX_DG,
     ["UN3480", "UN3090"], "Passenger aircraft only: lithium batteries shipped alone (UN3480, UN3090) are cargo-aircraft-only.",
     "Helsinki hub with belly capacity from Asia and to North America."),
    ("LUFTHANSA", "Lufthansa Cargo", ["air"], "DE", "partner_portal", "https://www.lufthansa-cargo.com", None, 87, None, AIR_CAO_DG, [], None,
     "Via Frankfurt with freighter capacity; trucked or flown to Helsinki."),
    ("DHLGF", "DHL Global Forwarding", ["air", "sea"], "DE", "dhl", "https://www.dhl.com/global-en/home/our-divisions/global-forwarding.html", None, 88, None,
     AIR_CAO_DG, [], None, "Air consolidation Asia–Helsinki and Helsinki–US; LCL Asia–Finland."),
    ("TURKISH", "Turkish Cargo", ["air"], "TR", "partner_portal", "https://www.turkishcargo.com", None, 84, None, AIR_CAO_DG, [], None,
     "Asia–Helsinki via Istanbul."),
    ("QATAR", "Qatar Airways Cargo", ["air"], "QA", "partner_portal", "https://www.qrcargo.com", None, 86, None, AIR_CAO_DG, [], None,
     "Asia–Helsinki via Doha."),
    ("VRTP", "VR Transpoint", ["rail"], "FI", "partner_portal", "https://www.vrtranspoint.fi", None, 94, 18.0, RAIL_DG, [], None,
     "Domestic container rail from Kotka/Hamina/Helsinki to Tampere, Kouvola and Oulu."),
    ("RCG", "Rail Cargo Group (Middle Corridor)", ["rail"], "AT", "manual", "https://www.railcargo.com", None, 72, 35.0, [], [],
     "Dangerous goods on the Middle Corridor only on request (several transshipments).",
     "China–Europe block trains on the Middle Corridor (Kazakhstan–Caspian–Georgia–Black Sea); avoids Russia and Belarus."),
    ("GREENCARGO", "Green Cargo", ["rail"], "SE", "partner_portal", "https://www.greencargo.com", None, 91, 15.0, RAIL_DG, [], None,
     "Swedish rail freight Gothenburg–Haparanda/Tornio for northern Finland."),
]


def _month_bounds(d: date, months_ahead: int) -> date:
    y, m = d.year, d.month + months_ahead
    while m > 12:
        y, m = y + 1, m - 12
    return date(y, m, calendar.monthrange(y, m)[1])


def windows(today: date) -> dict:
    q_start = date(today.year, today.month, 1)
    nq_month = ((today.month - 1) // 3 + 1) * 3 + 1
    nq = date(today.year + (1 if nq_month > 12 else 0), (nq_month - 1) % 12 + 1, 1)
    return {
        "ocean": (q_start, _month_bounds(today, 2)),
        "road": (date(today.year, today.month, 1) - timedelta(days=92), _month_bounds(today, 2)),
        "air": (today - timedelta(days=10), today + timedelta(days=12)),
        "expiring": (q_start, today + timedelta(days=9)),
        "expired": (today - timedelta(days=40), today - timedelta(days=6)),
        "upcoming": (nq, _month_bounds(nq, 2)),
    }


def S(code, basis, label=None, *, unit=None, by_size=None, pct=None, min_eur=None):
    s = {"code": code, "label": label or code, "basis": basis}
    if unit is not None:
        s["unit_cost_cents"] = C(unit)
    if by_size:
        s["by_size"] = {k: C(v) for k, v in by_size.items()}
    if pct is not None:
        s["pct"] = pct
    if min_eur is not None:
        s["min_cents"] = C(min_eur)
    return s


ASIA_FI_FCL = [
    S("BAF", "PER_CONTAINER", "Bunker adjustment (BAF)", by_size={"20": 340, "40": 680}),
    S("ETS", "PER_CONTAINER", "EU ETS surcharge (2026: 100 % of in-scope emissions)", by_size={"20": 82, "40": 164}),
    S("FUELEU", "PER_CONTAINER", "FuelEU Maritime", by_size={"20": 18, "40": 36}),
    S("OTHC", "PER_CONTAINER", "Origin THC (China/Asia)", by_size={"20": 115, "40": 175}),
    S("VGM", "PER_CONTAINER", "VGM / SOLAS", by_size={"20": 22, "40": 22}),
    S("ISPS", "PER_CONTAINER", "ISPS", by_size={"20": 12, "40": 12}),
    S("DOC", "PER_BL", "B/L fee", unit=55),
    S("DTHC", "PER_CONTAINER", "Destination THC (Finland)", by_size={"20": 195, "40": 245}),
    S("DO", "PER_BL", "Delivery order", unit=65),
]
ASIA_HUB_FCL = [s for s in ASIA_FI_FCL if s["code"] not in ("DTHC", "DO")] + [
    S("HUB_THC", "PER_CONTAINER", "Hub THC (discharge for feeder)", by_size={"20": 230, "40": 280})]
FEEDER_FI = [
    S("OTHC", "PER_CONTAINER", "Origin THC at the hub (local cargo only)", by_size={"20": 210, "40": 260}),
    S("BAF", "PER_CONTAINER", "Feeder BAF", by_size={"20": 85, "40": 170}),
    S("ETS", "PER_CONTAINER", "EU ETS (intra-EU voyage)", by_size={"20": 28, "40": 56}),
    S("FUELEU", "PER_CONTAINER", "FuelEU Maritime", by_size={"20": 8, "40": 16}),
    S("DTHC", "PER_CONTAINER", "Destination THC (Finland)", by_size={"20": 195, "40": 245}),
    S("ISPS", "PER_CONTAINER", "ISPS", by_size={"20": 12, "40": 12}),
    S("DO", "PER_BL", "Delivery order", unit=55),
]
FI_US_FCL = [
    S("OTHC", "PER_CONTAINER", "Origin THC (Finland)", by_size={"20": 195, "40": 245}),
    S("BAF", "PER_CONTAINER", "Bunker adjustment (BAF)", by_size={"20": 290, "40": 580}),
    S("ETS", "PER_CONTAINER", "EU ETS surcharge", by_size={"20": 60, "40": 120}),
    S("FUELEU", "PER_CONTAINER", "FuelEU Maritime", by_size={"20": 15, "40": 30}),
    S("VGM", "PER_CONTAINER", "VGM / SOLAS", by_size={"20": 15, "40": 15}),
    S("ISPS", "PER_CONTAINER", "ISPS", by_size={"20": 12, "40": 12}),
    S("DOC", "PER_BL", "B/L fee", unit=55),
    S("DTHC", "PER_CONTAINER", "Destination THC (US)", by_size={"20": 380, "40": 420}),
    S("DO", "PER_BL", "Delivery order (US)", unit=70),
]
LCL_ASIA = [
    S("CFS_O", "PER_WM", "Origin CFS", unit=22),
    S("ETS", "PER_WM", "EU ETS surcharge", unit=4),
    S("DOC", "PER_BL", "House B/L fee", unit=45),
    S("CFS_D", "PER_WM", "Destination CFS / devanning (Helsinki)", unit=46),
    S("DO", "PER_BL", "Delivery order", unit=55),
    S("ISPS", "PER_BL", "ISPS", unit=6),
]


def AIR_S(fsc, ssc=0.15, dest_handling=0.20, dest_min=45, awb=35):
    return [S("FSC", "PER_KG", "Fuel surcharge", unit=fsc), S("SSC", "PER_KG", "Security / screening", unit=ssc),
            S("AWB", "PER_SHIPMENT", "AWB fee", unit=awb),
            S("HANDLING_D", "PER_KG", "Destination airport handling", unit=dest_handling, min_eur=dest_min)]


AIR_TIERS = [45, 100, 300, 500, 1000, None]
ROAD_TIERS = [500, 1000, 2500, 5000, 10000, 24000]
DOM_TIERS = [100, 500, 1000, 2500, 5000, 10000, 24000]
FI_BANDS = [{"max_km": 100, "factor": 1.0}, {"max_km": 250, "factor": 1.3}, {"max_km": 450, "factor": 1.6},
            {"max_km": 700, "factor": 1.9}, {"max_km": None, "factor": 2.3}]
EU_BANDS = [{"max_km": 100, "factor": 1.0}, {"max_km": 300, "factor": 1.35}, {"max_km": 600, "factor": 1.7},
            {"max_km": None, "factor": 2.1}]
WEEKDAYS = [0, 1, 2, 3, 4]
EU_FTL = ["DE", "NL", "BE", "PL", "LT", "LV", "CZ", "AT", "FR", "IT", "DK"]


def tiers(breaks, eur):
    # per-kg tariffs keep 1/100 cent precision (e.g. 0.135 EUR/kg = 13.5 cents)
    return [{"max_qty": b, "unit_cost_cents": round(v * 100, 2)} for b, v in zip(breaks, eur)]


def ep(code=None, radius=None, country=None, countries=None):
    if code:
        return {"unlocode": code, "radius_km": radius if radius is not None else 60}
    if countries:
        return {"countries": countries}
    return {"country": country}


def build_cards(today: date) -> list[dict]:
    w = windows(today)
    cards = []

    def add(key, carrier, mode, service, origin, destination, *, name, basis, transit, window="ocean", via=None,
            costs=None, unit=None, tier=None, bands=None, min_eur=None, weekdays=None, surcharges=None, notes=None,
            distance_km=None, circuity=None, km_per_day=None):
        vf, vt = w[window]
        cards.append({
            "id": stable_id("rate", key), "carrier_id": carrier_id(carrier), "mode": mode, "service": service, "service_name": name,
            "origin": origin, "destination": destination, "via": via or [], "basis": basis,
            "unit_cost_cents": C(unit) if unit is not None else None,
            "container_costs": {k: C(v) for k, v in (costs or {}).items()} or None, "tiers": tier or None,
            "distance_bands": bands or None, "min_cost_cents": C(min_eur) if min_eur else None, "transit_days": transit,
            "departure_weekdays": weekdays if weekdays is not None else [], "surcharges": surcharges or [],
            "valid_from": vf.isoformat(), "valid_to": vt.isoformat(), "source": "indicative", "currency": "EUR",
            "notes": notes, "distance_km": distance_km, "circuity": circuity, "km_per_day": km_per_day,
            "archived": False, "demo": True, "created_at": now_iso(), "updated_at": now_iso(),
            "updated_by": {"type": "system", "name": "Seed (indicative 2026 levels)"},
        })

    def fcl(key, carrier, o, d, via, c20, c40, c40hc, transit, wd, name, sur=ASIA_FI_FCL, window="ocean", notes=None, ro=60, rd=40):
        add(key, carrier, "sea", "FCL", ep(o, ro), ep(d, rd), name=name, basis="PER_CONTAINER", transit=transit, window=window,
            via=via, costs={"20GP": c20, "40GP": c40, "40HC": c40hc}, weekdays=wd, surcharges=sur, notes=notes)

    # --- Asia -> Finland FCL, through rates incl. feeder ---------------------------------------------------
    fcl("maersk-sha-hel", "MAERSK", "CNSHA", "FIHEL", ["DEBRV"], 1320, 2180, 2240, 44, [3], "Asia–North Europe via Bremerhaven + feeder")
    fcl("maersk-ngb-hel", "MAERSK", "CNNGB", "FIHEL", ["DEBRV"], 1300, 2150, 2210, 45, [2], "Asia–North Europe via Bremerhaven + feeder")
    fcl("maersk-ytn-ktk", "MAERSK", "CNYTN", "FIKTK", ["DEBRV"], 1280, 2120, 2170, 41, [5], "Asia–North Europe via Bremerhaven + feeder")
    fcl("hapag-sha-hel", "HAPAG", "CNSHA", "FIHEL", ["DEHAM"], 1350, 2220, 2270, 43, [1], "Asia–North Europe via Hamburg + feeder")
    fcl("hapag-ytn-rau", "HAPAG", "CNYTN", "FIRAU", ["DEHAM"], 1330, 2190, 2240, 42, [4], "Asia–North Europe via Hamburg + feeder")
    fcl("hapag-sgn-hel", "HAPAG", "VNSGN", "FIHEL", ["SGSIN", "DEHAM"], 1240, 2050, 2100, 40, [0], "Vietnam–North Europe via Singapore and Hamburg")
    fcl("msc-ngb-ktk", "MSC", "CNNGB", "FIKTK", ["DEBRV"], 1220, 2020, 2070, 46, [6], "Asia–North Europe via Bremerhaven + feeder")
    fcl("msc-sha-hel", "MSC", "CNSHA", "FIHEL", ["PLGDN"], 1250, 2060, 2110, 42, [4], "Asia–Baltic via Gdańsk + feeder")
    fcl("msc-pus-hel", "MSC", "KRPUS", "FIHEL", ["DEHAM"], 1410, 2320, 2380, 48, [2], "Korea–North Europe via Hamburg + feeder (next quarter, incl. GRI)",
        window="upcoming", notes="Next-quarter tariff including the announced general rate increase.")
    fcl("msc-sin-rau", "MSC", "SGSIN", "FIRAU", ["BEANR"], 1120, 1860, 1900, 35, [1], "Singapore–North Europe via Antwerp + feeder")
    fcl("one-pus-hel", "ONE", "KRPUS", "FIHEL", ["NLRTM"], 1360, 2240, 2290, 47, [5], "Korea–North Europe via Rotterdam + feeder")
    fcl("one-sgn-ktk", "ONE", "VNSGN", "FIKTK", ["NLRTM"], 1210, 1990, 2040, 41, [3], "Vietnam–North Europe via Rotterdam + feeder")
    fcl("cma-sha-ktk", "CMACGM", "CNSHA", "FIKTK", ["NLRTM"], 1290, 2120, 2170, 44, [6], "Asia–North Europe via Rotterdam + Containerships feeder")
    fcl("cma-sin-hel", "CMACGM", "SGSIN", "FIHEL", ["NLRTM"], 1150, 1900, 1950, 34, [2], "Singapore–North Europe via Rotterdam + feeder",
        window="expiring", notes="Short validity: carrier announced a new tariff for next month.")
    fcl("cosco-sha-hel", "COSCO", "CNSHA", "FIHEL", ["DEHAM"], 1230, 2030, 2080, 44, [0], "Asia–North Europe via Hamburg + feeder")
    fcl("cosco-ngb-hel", "COSCO", "CNNGB", "FIHEL", ["PLGDN"], 1220, 2010, 2060, 45, [3], "Asia–Baltic via Gdańsk + feeder")
    # --- Asia -> hub (port-to-port) for merchant-arranged feeder ----------------------------------------------
    fcl("cosco-sha-ham", "COSCO", "CNSHA", "DEHAM", [], 1040, 1720, 1760, 36, [0], "Asia–Hamburg port-to-port", sur=ASIA_HUB_FCL, rd=30)
    fcl("one-ytn-rtm", "ONE", "CNYTN", "NLRTM", [], 990, 1640, 1690, 33, [3], "South China–Rotterdam port-to-port", sur=ASIA_HUB_FCL, rd=30)
    fcl("msc-ngb-gdn", "MSC", "CNNGB", "PLGDN", [], 1080, 1790, 1840, 38, [6], "Asia–Gdańsk port-to-port", sur=ASIA_HUB_FCL, rd=30)
    fcl("hapag-ngb-got", "HAPAG", "CNNGB", "SEGOT", [], 1080, 1800, 1850, 37, [2], "Asia–Gothenburg port-to-port", sur=ASIA_HUB_FCL, rd=30)
    # --- Feeders hub -> Finland --------------------------------------------------------------------------------
    fcl("unif-ham-hel", "UNIFEEDER", "DEHAM", "FIHEL", [], 420, 610, 630, 3, [1, 4], "Feeder Hamburg–Helsinki", sur=FEEDER_FI, ro=30)
    fcl("unif-ham-ktk", "UNIFEEDER", "DEHAM", "FIKTK", [], 440, 640, 660, 4, [2, 5], "Feeder Hamburg–Kotka", sur=FEEDER_FI, ro=30)
    fcl("unif-gdn-hel", "UNIFEEDER", "PLGDN", "FIHEL", [], 360, 520, 540, 2, [3], "Feeder Gdańsk–Helsinki", sur=FEEDER_FI, ro=30)
    fcl("unif-got-hel", "UNIFEEDER", "SEGOT", "FIHEL", [], 390, 560, 580, 3, [0, 3], "Feeder Gothenburg–Helsinki", sur=FEEDER_FI, ro=30)
    fcl("cs-rtm-ktk", "CONTAINERSHIPS", "NLRTM", "FIKTK", [], 470, 690, 710, 5, [2], "Feeder Rotterdam–Kotka", sur=FEEDER_FI, ro=30)
    fcl("cs-ham-hel", "CONTAINERSHIPS", "DEHAM", "FIHEL", [], 410, 600, 620, 3, [4], "Feeder Hamburg–Helsinki", sur=FEEDER_FI, ro=30)
    fcl("cs-ham-rau", "CONTAINERSHIPS", "DEHAM", "FIRAU", [], 450, 650, 670, 4, [1], "Feeder Hamburg–Rauma", sur=FEEDER_FI, ro=30)
    # --- Finland -> US FCL exports ------------------------------------------------------------------------------
    fcl("hapag-hel-nyc", "HAPAG", "FIHEL", "USNYC", ["DEHAM"], 1650, 2550, 2600, 21, [2], "Finland–US East Coast via Hamburg", sur=FI_US_FCL)
    fcl("msc-ktk-nyc", "MSC", "FIKTK", "USNYC", ["DEBRV"], 1560, 2420, 2470, 23, [4], "Finland–US East Coast via Bremerhaven", sur=FI_US_FCL)
    fcl("maersk-rau-sav", "MAERSK", "FIRAU", "USSAV", ["DEBRV"], 1720, 2690, 2740, 25, [1], "Finland–US Southeast via Bremerhaven", sur=FI_US_FCL)
    fcl("cma-hel-hou", "CMACGM", "FIHEL", "USHOU", ["NLRTM"], 1780, 2780, 2830, 28, [3], "Finland–US Gulf via Rotterdam", sur=FI_US_FCL)

    # --- LCL Asia -> Helsinki and Helsinki -> US ---------------------------------------------------------------------
    def lcl(key, carrier, o, d, via, per_wm, transit, wd, name, sur=LCL_ASIA):
        add(key, carrier, "sea", "LCL", ep(o, 60), ep(d, 40), name=name, basis="PER_WM", transit=transit, via=via,
            unit=per_wm, min_eur=per_wm, weekdays=wd, surcharges=sur)

    lcl("dhl-lcl-sha-hel", "DHLGF", "CNSHA", "FIHEL", ["DEHAM"], 68, 50, [4], "LCL consolidation Shanghai–Helsinki via Hamburg")
    lcl("sch-lcl-sha-hel", "SCHENKER", "CNSHA", "FIHEL", ["DEHAM"], 72, 49, [2], "LCL consolidation Shanghai–Helsinki via Hamburg")
    lcl("dsv-lcl-ngb-hel", "DSV", "CNNGB", "FIHEL", ["DEHAM"], 64, 52, [3], "LCL consolidation Ningbo–Helsinki via Hamburg")
    lcl("dhl-lcl-sgn-hel", "DHLGF", "VNSGN", "FIHEL", ["DEHAM"], 74, 47, [1], "LCL consolidation Ho Chi Minh City–Helsinki")
    lcl("sch-lcl-ytn-hel", "SCHENKER", "CNYTN", "FIHEL", ["NLRTM"], 70, 48, [4], "LCL consolidation Shenzhen–Helsinki via Rotterdam")
    lcl("dsv-lcl-sin-hel", "DSV", "SGSIN", "FIHEL", ["NLRTM"], 66, 44, [2], "LCL consolidation Singapore–Helsinki")
    lcl("dhl-lcl-hel-nyc", "DHLGF", "FIHEL", "USNYC", ["DEHAM"], 85, 30, [3], "LCL export Helsinki–New York",
        sur=[S("CFS_O", "PER_WM", "Origin CFS (Helsinki)", unit=38), S("ETS", "PER_WM", "EU ETS", unit=3),
             S("DOC", "PER_BL", "House B/L fee", unit=45), S("CFS_D", "PER_WM", "Destination CFS (US)", unit=55),
             S("DO", "PER_BL", "Delivery order (US)", unit=60)])

    # --- Air -------------------------------------------------------------------------------------------------------------
    def air(key, carrier, o, d, via, t, fsc, transit, wd, name, window="air", ssc=0.15, dest_handling=0.20, dest_min=45, ro=5, rd=40):
        add(key, carrier, "air", "AIR", ep(o, ro), ep(d, rd), name=name, basis="PER_KG", transit=transit, window=window, via=via,
            tier=tiers(AIR_TIERS, t), min_eur=95, weekdays=wd, surcharges=AIR_S(fsc, ssc, dest_handling, dest_min))

    air("ay-pvg-hel", "FINNAIR", "CNPVG", "FIHEL", [], [4.60, 3.90, 3.45, 3.20, 3.00, 2.85], 0.95, 2, [], "Shanghai PVG–Helsinki direct", ssc=0.16)
    air("ay-hkg-hel", "FINNAIR", "HKHKG", "FIHEL", [], [4.40, 3.75, 3.30, 3.05, 2.90, 2.75], 0.95, 2, [], "Hong Kong–Helsinki direct",
        window="expired", ssc=0.16)
    air("ay-sin-hel", "FINNAIR", "SGSIN", "FIHEL", [], [4.50, 3.80, 3.40, 3.15, 2.95, 2.80], 0.95, 2, [0, 2, 4, 6], "Singapore–Helsinki direct", ssc=0.16)
    air("lh-pvg-hel", "LUFTHANSA", "CNPVG", "FIHEL", ["DEFRA"], [4.20, 3.60, 3.20, 2.95, 2.75, 2.60], 1.05, 3, [], "Shanghai–Helsinki via Frankfurt")
    air("tk-pvg-hel", "TURKISH", "CNPVG", "FIHEL", ["TRIST"], [3.80, 3.20, 2.85, 2.60, 2.45, 2.30], 0.90, 4, [], "Shanghai–Helsinki via Istanbul", ssc=0.12)
    air("tk-hkg-hel", "TURKISH", "HKHKG", "FIHEL", ["TRIST"], [3.70, 3.10, 2.80, 2.55, 2.40, 2.25], 0.90, 4, [], "Hong Kong–Helsinki via Istanbul", ssc=0.12)
    air("qr-pvg-hel", "QATAR", "CNPVG", "FIHEL", ["QADOH"], [3.90, 3.30, 2.95, 2.70, 2.50, 2.35], 0.92, 4, [], "Shanghai–Helsinki via Doha")
    air("qr-sgn-hel", "QATAR", "VNSGN", "FIHEL", ["QADOH"], [4.00, 3.40, 3.00, 2.75, 2.55, 2.40], 0.92, 4, [], "Ho Chi Minh City–Helsinki via Doha", ro=20)
    air("dhl-pvg-hel", "DHLGF", "CNPVG", "FIHEL", [], [4.10, 3.50, 3.10, 2.85, 2.65, 2.50], 0.98, 4, [0, 2, 4], "Air consolidation Shanghai–Helsinki")
    air("ay-hel-nyc", "FINNAIR", "FIHEL", "USNYC", [], [3.60, 3.00, 2.60, 2.40, 2.25, 2.10], 0.85, 1, [], "Helsinki–New York JFK direct",
        dest_handling=0.25, dest_min=60, ro=40, rd=40)
    air("ay-hel-chi", "FINNAIR", "FIHEL", "USCHI", [], [3.70, 3.10, 2.70, 2.50, 2.35, 2.20], 0.85, 1, [1, 3, 5], "Helsinki–Chicago O'Hare direct",
        dest_handling=0.25, dest_min=60, ro=40, rd=40)
    air("lh-hel-chi", "LUFTHANSA", "FIHEL", "USCHI", ["DEFRA"], [3.40, 2.85, 2.50, 2.30, 2.15, 2.00], 1.00, 2, [], "Helsinki–Chicago via Frankfurt",
        dest_handling=0.25, dest_min=60, ro=40, rd=40)
    air("dhl-hel-nyc", "DHLGF", "FIHEL", "USNYC", [], [3.50, 2.90, 2.55, 2.35, 2.20, 2.05], 0.90, 3, [1, 3], "Air consolidation Helsinki–New York",
        dest_handling=0.25, dest_min=60, ro=40, rd=40)

    # --- Road groupage (LTL) and FTL, Europe <-> Finland ----------------------------------------------------------------
    def ltl(key, carrier, o, d, ro, rd, t, min_eur, fuel, transit, name, via=None, window="road"):
        add(key, carrier, "road", "ROAD_LTL", ep(o, ro), ep(d, rd), name=name, basis="PER_KG", transit=transit, window=window,
            via=via, tier=tiers(ROAD_TIERS, t), min_eur=min_eur, weekdays=WEEKDAYS,
            surcharges=[S("ROAD_FUEL", "PERCENT", f"Fuel surcharge {fuel} %", pct=fuel)])

    ltl("dsv-ham-hel", "DSV", "DEHAM", "FIHEL", 120, 50, [0.40, 0.27, 0.175, 0.13, 0.10, 0.075], 120, 14, 4, "Groupage Hamburg–Helsinki via Travemünde ferry", ["DETRV"])
    ltl("dsv-rtm-hel", "DSV", "NLRTM", "FIHEL", 120, 50, [0.45, 0.30, 0.195, 0.145, 0.11, 0.083], 135, 14, 5, "Groupage Benelux–Helsinki via Travemünde ferry", ["DETRV"])
    ltl("dsv-sto-hel", "DSV", "SESTO", "FIHEL", 120, 50, [0.32, 0.22, 0.145, 0.11, 0.085, 0.063], 95, 12, 2, "Groupage Stockholm–Helsinki ferry")
    ltl("sch-ham-hel", "SCHENKER", "DEHAM", "FIHEL", 150, 50, [0.41, 0.28, 0.18, 0.135, 0.104, 0.078], 125, 15, 4, "Groupage Northern Germany–Helsinki via Travemünde", ["DETRV"])
    ltl("sch-dui-hel", "SCHENKER", "DEDUI", "FIHEL", 150, 50, [0.47, 0.32, 0.205, 0.155, 0.118, 0.088], 140, 15, 5, "Groupage Rhine-Ruhr–Helsinki via Travemünde", ["DETRV"])
    ltl("sch-waw-hel", "SCHENKER", "PLWAW", "FIHEL", 150, 50, [0.43, 0.29, 0.185, 0.14, 0.106, 0.08], 125, 15, 4, "Groupage Poland–Helsinki via Via Baltica and Tallinn", ["EETLL"])
    ltl("kk-tll-hel", "KAUKOKIITO", "EETLL", "FIHEL", 80, 50, [0.24, 0.17, 0.115, 0.09, 0.07, 0.055], 65, 9, 1, "Groupage Tallinn–Helsinki ferry")
    ltl("dsv-tll-hel", "DSV", "EETLL", "FIHEL", 80, 50, [0.26, 0.18, 0.12, 0.092, 0.071, 0.056], 70, 9, 1, "Groupage Estonia–Finland, daily ferry")
    ltl("sch-tll-hel", "SCHENKER", "EETLL", "FIHEL", 80, 50, [0.25, 0.175, 0.118, 0.091, 0.07, 0.055], 68, 9, 2, "Groupage Tallinn–Helsinki (Vantaa terminal)")
    ltl("pn-got-hel", "POSTNORD", "SEGOT", "FIHEL", 120, 50, [0.37, 0.25, 0.165, 0.125, 0.095, 0.07], 105, 12, 3, "Groupage Gothenburg–Helsinki via Stockholm–Turku", ["SESTO", "FITKU"])
    ltl("pn-cph-hel", "POSTNORD", "DKCPH", "FIHEL", 100, 50, [0.43, 0.29, 0.185, 0.14, 0.106, 0.08], 125, 12, 4, "Groupage Denmark–Helsinki via Stockholm–Turku", ["SESTO", "FITKU"])
    ltl("pn-sto-tku", "POSTNORD", "SESTO", "FITKU", 120, 60, [0.30, 0.21, 0.14, 0.105, 0.08, 0.06], 90, 12, 2, "Groupage Stockholm–Turku ferry")
    ltl("dsv-hel-ham", "DSV", "FIHEL", "DEHAM", 60, 120, [0.38, 0.26, 0.17, 0.125, 0.096, 0.072], 115, 14, 4, "Groupage Helsinki–Hamburg via Travemünde", ["DETRV"])
    ltl("sch-hel-ham", "SCHENKER", "FIHEL", "DEHAM", 60, 150, [0.39, 0.265, 0.172, 0.128, 0.098, 0.074], 118, 15, 4, "Groupage Helsinki–Northern Germany via Travemünde", ["DETRV"])
    ltl("pn-tku-sto", "POSTNORD", "FITKU", "SESTO", 60, 120, [0.29, 0.20, 0.135, 0.10, 0.077, 0.058], 85, 12, 2, "Groupage Turku–Stockholm ferry")
    ltl("pn-hel-got", "POSTNORD", "FIHEL", "SEGOT", 60, 120, [0.35, 0.24, 0.16, 0.12, 0.092, 0.068], 100, 12, 3, "Groupage Helsinki–Gothenburg via Turku–Stockholm", ["FITKU", "SESTO"])
    ltl("kk-hel-tll", "KAUKOKIITO", "FIHEL", "EETLL", 50, 80, [0.23, 0.165, 0.11, 0.087, 0.067, 0.053], 62, 9, 1, "Groupage Helsinki–Tallinn ferry")
    ltl("dsv-hel-tll", "DSV", "FIHEL", "EETLL", 50, 80, [0.25, 0.175, 0.116, 0.09, 0.069, 0.054], 68, 9, 1, "Groupage Finland–Estonia, daily ferry")

    add("dsv-ftl-ham-hel", "DSV", "road", "ROAD_FTL", ep("DEHAM", 150), ep("FIHEL", 80), name="Full truck Hamburg–Helsinki via Travemünde",
        basis="PER_SHIPMENT", unit=2150, transit=3, window="road", via=["DETRV"], weekdays=WEEKDAYS,
        surcharges=[S("ROAD_FUEL", "PERCENT", "Fuel surcharge 14 %", pct=14)])
    add("dsv-ftl-hel-ham", "DSV", "road", "ROAD_FTL", ep("FIHEL", 80), ep("DEHAM", 150), name="Full truck Helsinki–Hamburg via Travemünde",
        basis="PER_SHIPMENT", unit=1850, transit=3, window="road", via=["DETRV"], weekdays=WEEKDAYS,
        surcharges=[S("ROAD_FUEL", "PERCENT", "Fuel surcharge 14 %", pct=14)])
    add("girteka-eu-fi", "GIRTEKA", "road", "ROAD_FTL", ep(countries=EU_FTL), ep(country="FI"),
        name="Full truck continental Europe–Finland via Via Baltica and Tallinn–Helsinki", basis="PER_KM", unit=1.38, min_eur=1100,
        transit=1, km_per_day=650, window="road", weekdays=WEEKDAYS, via=["EETLL", "FIHEL"],
        surcharges=[S("FERRY", "PER_TRUCK", "Tallinn–Helsinki ferry", unit=520)])
    add("girteka-fi-eu", "GIRTEKA", "road", "ROAD_FTL", ep(country="FI"), ep(countries=EU_FTL),
        name="Full truck Finland–continental Europe via Helsinki–Tallinn and Via Baltica", basis="PER_KM", unit=1.30, min_eur=1000,
        transit=1, km_per_day=650, window="road", weekdays=WEEKDAYS, via=["FIHEL", "EETLL"],
        surcharges=[S("FERRY", "PER_TRUCK", "Helsinki–Tallinn ferry", unit=520)])

    # --- Ro-ro / ro-pax, unaccompanied trailers per lane metre ------------------------------------------------------------
    def roro(key, carrier, o, d, per_lm, ets_lm, transit, name, trailer, wd=None, baf=8):
        add(key, carrier, "sea", "RORO", ep(o, 15), ep(d, 15), name=name, basis="PER_LDM", transit=transit, window="road",
            tier=[{"max_qty": None, "unit_cost_cents": C(per_lm)}], weekdays=wd or [],
            surcharges=[S("ETS", "PER_LDM", "EU ETS surcharge", unit=ets_lm), S("BAF", "PERCENT", f"Bunker surcharge {baf} %", pct=baf),
                        S("TRAILER", "PER_TRUCK", "Trailer hire, terminal tractor and port-area delivery", unit=trailer)])

    roro("fl-trv-hel", "FINNLINES", "DETRV", "FIHEL", 72, 3.2, 1, "Ro-ro Travemünde–Helsinki, unaccompanied trailer", 420)
    roro("fl-hel-trv", "FINNLINES", "FIHEL", "DETRV", 64, 3.2, 1, "Ro-ro Helsinki–Travemünde, unaccompanied trailer", 380)
    roro("fl-kps-nli", "FINNLINES", "SEKPS", "FINLI", 46, 1.8, 1, "Ro-pax Kapellskär–Naantali, unaccompanied trailer", 300)
    roro("tal-tll-hel", "TALLINK", "EETLL", "FIHEL", 36, 1.2, 1, "Ro-pax Tallinn–Helsinki shuttle, unaccompanied trailer", 240)
    roro("eck-tll-hel", "ECKERO", "EETLL", "FIHEL", 33, 1.1, 1, "Ro-pax Tallinn–Helsinki (Finbo Cargo), unaccompanied trailer", 240)
    roro("vik-sto-tku", "VIKING", "SESTO", "FITKU", 44, 1.8, 1, "Ro-pax Stockholm–Turku via Åland, unaccompanied trailer", 300)
    roro("vik-tll-hel", "VIKING", "EETLL", "FIHEL", 35, 1.2, 1, "Ro-pax Tallinn–Helsinki, unaccompanied trailer", 240)
    roro("dfds-pla-kps", "DFDS", "EEPLA", "SEKPS", 41, 1.6, 1, "Ro-ro Paldiski–Kapellskär, unaccompanied trailer", 280)

    # --- Domestic tariffs (used for domestic shipments and as pre/on-carriage) ---------------------------------------------
    def dom_ltl(key, carrier, country, t, min_eur, fuel, bands, name):
        add(key, carrier, "road", "ROAD_LTL", ep(country=country), ep(country=country), name=name, basis="PER_KG",
            transit=0, km_per_day=600, window="road", tier=tiers(DOM_TIERS, t), bands=bands, min_eur=min_eur, weekdays=WEEKDAYS,
            surcharges=[S("ROAD_FUEL", "PERCENT", f"Fuel surcharge {fuel} %", pct=fuel)])

    dom_ltl("kk-fi-ltl", "KAUKOKIITO", "FI", [0.38, 0.19, 0.135, 0.095, 0.072, 0.056, 0.042], 42, 8, FI_BANDS, "Finland domestic groupage")
    dom_ltl("posti-fi-ltl", "POSTI", "FI", [0.40, 0.18, 0.13, 0.092, 0.070, 0.054, 0.041], 39, 9,
            [{"max_km": 100, "factor": 1.0}, {"max_km": 250, "factor": 1.35}, {"max_km": 450, "factor": 1.65},
             {"max_km": 700, "factor": 1.95}, {"max_km": None, "factor": 2.4}], "Finland domestic pallets and groupage")
    dom_ltl("dsv-de-ltl", "DSV", "DE", [0.30, 0.16, 0.115, 0.085, 0.065, 0.050, 0.038], 45, 14, EU_BANDS, "Germany domestic groupage")
    dom_ltl("dsv-nl-ltl", "DSV", "NL", [0.31, 0.165, 0.12, 0.088, 0.067, 0.052, 0.039], 46, 14, EU_BANDS, "Benelux domestic groupage")
    dom_ltl("pn-se-ltl", "POSTNORD", "SE", [0.33, 0.17, 0.125, 0.09, 0.07, 0.054, 0.041], 48, 12, EU_BANDS, "Sweden domestic groupage")
    dom_ltl("sch-pl-ltl", "SCHENKER", "PL", [0.22, 0.12, 0.085, 0.065, 0.050, 0.038, 0.030], 30, 15, EU_BANDS, "Poland domestic groupage")
    dom_ltl("kk-ee-ltl", "KAUKOKIITO", "EE", [0.24, 0.13, 0.09, 0.068, 0.052, 0.040, 0.032], 32, 9, EU_BANDS, "Estonia domestic groupage")

    def dom_km(key, carrier, country, service, per_km, min_eur, fuel, name):
        add(key, carrier, "road", service, ep(country=country), ep(country=country), name=name, basis="PER_KM", unit=per_km,
            min_eur=min_eur, transit=0, km_per_day=600, window="road", weekdays=WEEKDAYS,
            surcharges=[S("ROAD_FUEL", "PERCENT", f"Fuel surcharge {fuel} %", pct=fuel)] if fuel else [])

    dom_km("kk-fi-ftl", "KAUKOKIITO", "FI", "ROAD_FTL", 1.75, 420, 8, "Finland domestic full truck")
    dom_km("cs-fi-haul", "CONTAINERSHIPS", "FI", "HAULAGE", 2.70, 210, 10, "Finland container haulage (incl. empty return)")
    dom_km("dsv-de-ftl", "DSV", "DE", "ROAD_FTL", 1.60, 380, 14, "Germany domestic full truck")
    dom_km("dsv-de-haul", "DSV", "DE", "HAULAGE", 2.40, 260, 14, "Germany container haulage")
    dom_km("pn-se-ftl", "POSTNORD", "SE", "ROAD_FTL", 1.70, 400, 12, "Sweden domestic full truck")

    # --- Rail -----------------------------------------------------------------------------------------------------------------
    def rail(key, carrier, o, d, via, c20, c40, transit, wd, name, sur=None, distance_km=None, window="road", ro=25, rd=30):
        add(key, carrier, "rail", "RAIL_FCL", ep(o, ro), ep(d, rd), name=name, basis="PER_CONTAINER", transit=transit, window=window,
            via=via, costs={"20GP": c20, "40GP": c40, "40HC": c40}, weekdays=wd, distance_km=distance_km,
            surcharges=sur if sur is not None else [S("DTHC", "PER_CONTAINER", "Rail terminal lift", by_size={"20": 65, "40": 65})])

    rail("vr-ktk-tmp", "VRTP", "FIKTK", "FITMP", [], 390, 520, 1, WEEKDAYS, "Container rail Kotka–Tampere")
    rail("vr-ktk-oul", "VRTP", "FIKTK", "FIOUL", [], 690, 890, 2, WEEKDAYS, "Container rail Kotka–Oulu")
    rail("vr-hmn-kvo", "VRTP", "FIHMN", "FIKVO", [], 260, 350, 1, WEEKDAYS, "Container rail Hamina–Kouvola")
    rail("vr-hel-oul", "VRTP", "FIHEL", "FIOUL", [], 720, 920, 2, [0, 2, 4], "Container rail Helsinki (Vuosaari)–Oulu")
    rail("gc-got-tor", "GREENCARGO", "SEGOT", "FITOR", ["SEHAA"], 820, 1150, 3, [1, 3], "Container rail Gothenburg–Haparanda/Tornio")
    rail("rcg-cdu-hel", "RCG", "CNCDU", "FIHEL", ["KZHRG", "AZBAK", "GEPTI", "ROCND"], 4300, 6800, 36, [2],
         "Middle Corridor rail Chengdu–Caspian–Black Sea + feeder to Helsinki", distance_km=11800, window="ocean", ro=60, rd=40,
         sur=[S("CASPIAN", "PER_CONTAINER", "Caspian ferry and transshipments", by_size={"20": 380, "40": 560}),
              S("DOC", "PER_BL", "Rail waybill / documents", unit=60),
              S("DTHC", "PER_CONTAINER", "Destination THC (Finland)", by_size={"20": 195, "40": 245})])
    return cards


def build_carriers() -> list[dict]:
    out = []
    for (code, name, modes, country, itype, web, email, ontime, co2, dg, dg_block, dg_note, note) in CARRIERS:
        out.append({
            "id": carrier_id(code), "name": name, "code": code, "modes": modes, "country": country,
            "integration": {"type": itype, "status": "not_configured"}, "contact_email": email, "website": web,
            "on_time_pct": float(ontime), "co2_factor_g_tkm": co2, "dg_classes_accepted": dg, "dg_un_blocked": dg_block,
            "dg_note": dg_note, "services_note": note, "active": True, "demo": True, "created_at": now_iso(), "updated_at": now_iso(),
        })
    return out


@on_startup(order=20)
async def seed_rates():
    if not config.SEED_DEMO or await db.system.find_one({"key": SEED_KEY}):
        return
    for c in build_carriers():
        await db.carriers.update_one({"id": c["id"]}, {"$setOnInsert": c}, upsert=True)
    for card in build_cards(now().date()):
        await db.rate_cards.update_one({"id": card["id"]}, {"$setOnInsert": card}, upsert=True)
    await db.system.update_one({"key": SEED_KEY}, {"$set": {"at": now_iso()}}, upsert=True)
