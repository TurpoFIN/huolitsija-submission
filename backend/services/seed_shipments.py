"""Demo shipments (startup order 30, only when SEED_DEMO; idempotent via db.system "seed:shipments:v1").

41 shipments for the 8 demo companies across sea / road / air / rail with real lane geometry, statuses spread over the
whole life cycle and times relative to now so they move on the map. Everything carries demo=True.

Real-world anchoring at seed time:
- Baltic sea legs into Finnish ports get a real vessel that is currently heading there (Digitraffic port calls +
  vessel metadata), so their positions come from live AIS; the leg ETA is the agent-reported port-call ETA.
- One domestic rail shipment rides a VR/Fenniarail/Operail cargo train that is running right now (rata.digitraffic.fi),
  so its position is the train's own GPS.
Supplier names, cargo and references are illustrative demo data; carrier names are looked up in the `carriers`
collection (V2, order 20) and fall back to plain names until V2 has seeded them.

The partner demo user (partner.demo@huolitsija.fi, role partner) is created only when DEMO_PARTNER_PASSWORD is set.
"""
from __future__ import annotations

import logging
import random
import re
import uuid
from datetime import date, timedelta

import httpx

from core import config
from core.db import db
from core.lifecycle import on_startup
from core.util import NO_ID, iso, new_id, now, now_iso, parse_iso
from services import geo, positions
from services import shipments as svc
from services.seed_core import DEMO_COMPANIES, demo_company_id

log = logging.getLogger("seed.shipments")
MARKER = "seed:shipments:v2"
OLD_MARKERS = ["seed:shipments:v1"]
H = {"Digitraffic-User": "Huolitsija/1.0", "Accept-Encoding": "gzip"}
OPS = {"type": "user", "id": None, "name": "Huolitsija", "org_type": "internal"}
PARTNER_EMAIL = "partner.demo@huolitsija.fi"
FALLBACK_PARTNER_CARRIER = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "huolitsija-demo-carrier-uudenmaan-rahti")),
                            "name": "Uudenmaan Rahti Oy", "code": "URAHTI"}


# --------------------------------------------------------------------------------------------- identifiers

def container_no(owner: str, rnd: random.Random) -> str:
    """ISO 6346 container number with a valid check digit (owner code + U + 6 digits + check)."""
    serial = f"{rnd.randint(0, 999999):06d}"
    base = (owner[:3] + "U" + serial).upper()
    values, v = {}, 10
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if v % 11 == 0:
            v += 1
        values[ch] = v
        v += 1
    total = sum((values[c] if c.isalpha() else int(c)) * (2 ** i) for i, c in enumerate(base))
    return base + str((total % 11) % 10)


def awb_no(prefix: str, rnd: random.Random) -> str:
    serial = rnd.randint(1000000, 9999999)
    return f"{prefix}-{serial}{serial % 7}"


# --------------------------------------------------------------------------------------------- live anchors

async def _get_json(url: str, params: dict | None = None, timeout: float = 40):
    async with httpx.AsyncClient(timeout=timeout, headers=H) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        return r.json()


async def find_vessels() -> dict[str, list[dict]]:
    """Upcoming arrivals (ETA 6 h .. 4 days ahead) at Finnish ports: {locode: [{name, mmsi, imo, prev, eta, type}]}."""
    out: dict[str, list[dict]] = {}
    if not config.DIGITRAFFIC_ENABLED:
        return out
    meta_cache: dict[str, dict] = {}
    try:  # which vessels are inside the Digitraffic AIS coverage right now (fresh fix in the last hour)
        feats = (await _get_json("https://meri.digitraffic.fi/api/ais/v1/locations", timeout=60)).get("features") or []
        in_ais = {str(f["mmsi"]) for f in feats if (now().timestamp() - (f["properties"].get("timestampExternal") or 0) / 1000) < 3600}
    except Exception:  # noqa: BLE001
        in_ais = set()
    for loc in ["FIHEL", "FIKTK", "FIHKO", "FIRAU", "FITKU", "FINLI", "FIOUL", "FIHMN"]:
        try:
            calls = (await _get_json("https://meri.digitraffic.fi/api/port-call/v1/port-calls", {"locode": loc})).get("portCalls") or []
        except Exception as exc:  # noqa: BLE001
            log.info("port calls %s: %s", loc, exc)
            continue
        for pc in calls:
            for a in pc.get("portAreaDetails") or []:
                eta = parse_iso(a.get("eta"))
                if not eta or a.get("ata") or not (now() + timedelta(hours=2) <= eta <= now() + timedelta(days=4)):
                    continue
                if (pc.get("prevPort") or "")[:2] in ("RU", "BY"):
                    continue  # sanctions: never attach demo cargo to a voyage from Russia/Belarus
                mmsi = pc.get("mmsi") or 0
                imo = pc.get("imoLloyds")
                if not mmsi and imo:
                    mmsi = await _mmsi_for_imo(imo, meta_cache)
                if not mmsi:
                    continue
                out.setdefault(loc, []).append({"name": pc.get("vesselName"), "mmsi": str(mmsi), "imo": str(imo) if imo else None,
                                                "prev": pc.get("prevPort"), "eta": iso(eta), "type": pc.get("vesselTypeCode"),
                                                "area": a.get("portAreaName"), "port_call_id": pc.get("portCallId"),
                                                "in_ais": str(mmsi) in in_ais})
    for v in out.values():
        v.sort(key=lambda x: x["eta"])
    return out


async def _mmsi_for_imo(imo, cache: dict) -> int | None:
    if "all" not in cache:
        try:
            cache["all"] = {str(v.get("imo")): v.get("mmsi") for v in await _get_json("https://meri.digitraffic.fi/api/ais/v1/vessels", timeout=60)}
        except Exception:  # noqa: BLE001
            cache["all"] = {}
    return cache["all"].get(str(imo))


HUBS = {"NLRTM", "DEBRV", "DEHAM", "BEANR", "PLGDN", "SEGOT"}


def pick_vessel(vessels: dict, port: str, *, prev_prefer: list[str], types: set, used: set, max_hours: float = 96) -> dict | None:
    limit = iso(now() + timedelta(hours=max_hours))
    cands = [v for v in vessels.get(port, []) if v["mmsi"] not in used and (not types or v.get("type") in types) and v["eta"] <= limit]
    rank = {p: i for i, p in enumerate(prev_prefer)}
    # vessels already inside AIS coverage first (their position is live from the start), then the preferred previous port
    cands.sort(key=lambda v: (not v.get("in_ais"), rank.get((v.get("prev") or "").upper(), 99), v["eta"]))
    if cands:
        used.add(cands[0]["mmsi"])
        return cands[0]
    return None


async def find_running_cargo_train() -> dict | None:
    """A cargo train running now with GPS, its timetable and real station coordinates."""
    if not config.DIGITRAFFIC_ENABLED:
        return None
    try:
        rata = "https://rata.digitraffic.fi/api/v1"
        locs = await _get_json(f"{rata}/train-locations/latest/")
        with_gps = {x["trainNumber"]: x for x in locs}
        trains = await _get_json(f"{rata}/trains/{date.today().isoformat()}")
        stations = {s["stationShortCode"]: s for s in await _get_json(f"{rata}/metadata/stations")}
    except Exception as exc:  # noqa: BLE001
        log.info("rail lookup failed: %s", exc)
        return None
    best = None
    for t in trains:
        if t.get("trainCategory") != "Cargo" or t["trainNumber"] not in with_gps or t.get("cancelled"):
            continue
        rows = t["timeTableRows"]
        a, b = rows[0], rows[-1]
        start, end = parse_iso(a["scheduledTime"]), parse_iso(b["scheduledTime"])
        if not (start and end and start < now() < end - timedelta(minutes=45)):
            continue
        if a["stationShortCode"] not in stations or b["stationShortCode"] not in stations:
            continue
        seq = []
        for r in rows:
            c = r["stationShortCode"]
            if c in stations and (not seq or seq[-1] != c):
                seq.append(c)
        km = geo.path_length_km([[stations[c]["longitude"], stations[c]["latitude"]] for c in seq])
        if km < 80:
            continue
        cand = {"number": str(t["trainNumber"]), "departure_date": t["departureDate"], "operator": t.get("operatorShortCode"),
                "start": iso(start), "end": iso(end), "km": km,
                "from": {"name": stations[seq[0]]["stationName"], "code": f"FI:{seq[0]}", "unlocode": None, "country": "FI",
                         "lat": stations[seq[0]]["latitude"], "lon": stations[seq[0]]["longitude"]},
                "to": {"name": stations[seq[-1]]["stationName"], "code": f"FI:{seq[-1]}", "unlocode": None, "country": "FI",
                       "lat": stations[seq[-1]]["latitude"], "lon": stations[seq[-1]]["longitude"]},
                "path": [[round(stations[c]["longitude"], 6), round(stations[c]["latitude"], 6)] for c in seq]}
        if best is None or km > best["km"]:
            best = cand
    return best


# --------------------------------------------------------------------------------------------- carriers

async def carrier_ref(name: str | None) -> tuple[str | None, str | None]:
    if not name:
        return None, None
    try:
        c = await db.carriers.find_one({"$or": [{"name": {"$regex": f"^{re.escape(name)}", "$options": "i"}},
                                                {"code": name.upper()}]}, {"_id": 0, "id": 1, "name": 1})
    except Exception:  # noqa: BLE001
        c = None
    return (c["id"], c["name"]) if c else (None, name)


async def partner_carrier() -> dict:
    """The road carrier the partner demo user works for: V2's road carrier if seeded, else a fictional haulier."""
    try:
        c = await db.carriers.find_one({"modes": ["road"], "integration.type": "partner_portal", "country": "FI"}, NO_ID) \
            or await db.carriers.find_one({"modes": ["road"], "active": {"$ne": False}}, NO_ID) \
            or await db.carriers.find_one({"modes": "road", "active": {"$ne": False}}, NO_ID)
    except Exception:  # noqa: BLE001
        c = None
    if c:
        return {"id": c["id"], "name": c["name"], "code": c.get("code")}
    return dict(FALLBACK_PARTNER_CARRIER)


# --------------------------------------------------------------------------------------------- specs

def S(mode, frm, to, role="main", **kw):
    return svc.leg_spec(mode, frm, to, role, **kw)


def addr(street, postal, city, cc):
    return {"street": street, "postal_code": postal, "city": city, "country": cc}


COMPANY_ADDR = {c["key"]: addr(c["street"], c["postal_code"], c["city"], "FI") for c in DEMO_COMPANIES}
COMPANY_NAME = {c["key"]: c["name"] for c in DEMO_COMPANIES}


def consignee(key, contact):
    return {"name": COMPANY_NAME[key], "address": COMPANY_ADDR[key], "contact_name": contact, "phone": None,
            "opening_hours": "Mon–Fri 7–15", "instructions": "Call 30 min before arrival; forklift on site"}


def shipper(name, street, postal, city, cc, contact=None):
    return {"name": name, "address": addr(street, postal, city, cc), "contact_name": contact}


# Each spec: key, company, mode, direction, incoterm(+place), legs, cargo, target status + anchor, parties, refs.
def specs() -> list[dict]:
    return [
        # ---------------- Pohjolan Kaluste Oy (Lahti): furniture
        {"key": "K1", "company": "kaluste", "mode": "sea", "incoterm": "FOB", "place": "Ningbo",
         "legs": [S("sea", "CNNGB", "DEHAM", carrier="Hapag-Lloyd", service="FE2 Asia–North Europe"),
                  S("sea", "DEHAM", "FIKTK", "feeder", carrier="Hapag-Lloyd", service="Baltic feeder"),
                  S("road", "FIKTK", "FILHI", "delivery", partner=True, to_address=COMPANY_ADDR["kaluste"])],
         "cargo": {"description": "Upholstered sofas and armchairs, flat-packed", "packages": 412, "package_type": "cartons",
                   "weight_kg": 14850, "volume_m3": 136, "containers": [{"type": "40HC", "count": 2}], "hs_hint": "9401"},
         "owner": "HLX", "target": ("in_transit", {"leg": 1, "f": 0.46}),
         "shipper": shipper("Ningbo Beilun Home Furnishing Co., Ltd.", "No. 88 Huashan Road", "315800", "Ningbo", "CN"),
         "refs": {"customer_ref": "PK-2026-0419", "po_numbers": ["PO-77120", "PO-77121"]}},
        {"key": "K2", "company": "kaluste", "mode": "sea", "incoterm": "FOB", "place": "Shanghai",
         "legs": [S("sea", "CNSHA", "DEBRV", carrier="Maersk", service="AE7 Asia–Europe"),
                  S("sea", "DEBRV", "FIHEL", "feeder", carrier="Maersk", service="Baltic feeder", live_vessel=True),
                  S("road", "FIHEL", "FILHI", "delivery", partner=True, to_address=COMPANY_ADDR["kaluste"])],
         "cargo": {"description": "Oak dining tables and chairs", "packages": 236, "package_type": "cartons", "weight_kg": 9620,
                   "volume_m3": 66, "containers": [{"type": "40HC", "count": 1}], "hs_hint": "9403"},
         "owner": "MSK", "target": ("live_vessel", {"leg": 2}),
         "shipper": shipper("Shanghai Pinewood Furniture Manufacturing Co.", "1200 Jiasong Road", "201700", "Shanghai", "CN"),
         "refs": {"customer_ref": "PK-2026-0402", "po_numbers": ["PO-77098"]}},
        {"key": "K3", "company": "kaluste", "mode": "road", "incoterm": "FCA", "place": "Poznań",
         "legs": [S("road", "PLPOZ", "EEMUG", "pickup", carrier="Girteka", service="FTL via Via Baltica",
                    from_address=addr("ul. Gdyńska 52", "61-016", "Poznań", "PL")),
                  S("sea", "EEMUG", "FIHEL", carrier="Eckerö Line (Finbo Cargo)", service="Ro-pax, accompanied trailer Muuga–Vuosaari"),
                  S("road", "FIHEL", "FILHI", "delivery", carrier="Girteka", to_address=COMPANY_ADDR["kaluste"])],
         "cargo": {"description": "Beech veneer furniture components", "packages": 26, "package_type": "pallets", "weight_kg": 11200,
                   "volume_m3": 58, "ldm": 13.6, "hs_hint": "9403"},
         "target": ("in_transit", {"leg": 1, "f": 0.55}),
         "shipper": shipper("Wielkopolska Meble Komponenty Sp. z o.o.", "ul. Gdyńska 52", "61-016", "Poznań", "PL"),
         "refs": {"customer_ref": "PK-2026-0433", "po_numbers": ["PO-77140"]}},
        {"key": "K4", "company": "kaluste", "mode": "road", "incoterm": "DAP", "place": "Lahti",
         "legs": [S("road", "LTKUN", "EEMUG", "pickup", carrier="Girteka", service="LTL groupage via Via Baltica",
                    from_address=addr("Raudondvario pl. 162", "47175", "Kaunas", "LT")),
                  S("sea", "EEMUG", "FIHEL", carrier="Eckerö Line (Finbo Cargo)", service="Ro-pax, accompanied trailer Muuga–Vuosaari"),
                  S("road", "FIHEL", "FILHI", "delivery", carrier="Girteka", to_address=COMPANY_ADDR["kaluste"])],
         "cargo": {"description": "Upholstery foam and textile rolls", "packages": 14, "package_type": "pallets", "weight_kg": 3400,
                   "volume_m3": 31, "ldm": 6.4},
         "target": ("booked", {"days": 2.3}),
         "shipper": shipper("Baltic Upholstery Supplies UAB", "Raudondvario pl. 162", "47175", "Kaunas", "LT"),
         "refs": {"customer_ref": "PK-2026-0441", "po_numbers": ["PO-77152"]}},
        {"key": "K5", "company": "kaluste", "mode": "sea", "incoterm": "FOB", "place": "Ho Chi Minh City",
         "legs": [S("sea", "VNSGN", "NLRTM", carrier="CMA CGM", service="FAL3 French Asia Line"),
                  S("sea", "NLRTM", "FIHEL", "feeder", carrier="CMA CGM", service="Baltic feeder"),
                  S("road", "FIHEL", "FILHI", "delivery", partner=True, to_address=COMPANY_ADDR["kaluste"])],
         "cargo": {"description": "Rattan lounge chairs (LCL)", "packages": 64, "package_type": "cartons", "weight_kg": 2140,
                   "volume_m3": 18.5, "hs_hint": "9401"},
         "owner": "CMA", "target": ("delivered", {"days": 6}),
         "shipper": shipper("Saigon Rattan Craft JSC", "45 Nguyen Van Linh", "700000", "Ho Chi Minh City", "VN"),
         "refs": {"customer_ref": "PK-2026-0351", "po_numbers": ["PO-76988"]}},
        {"key": "K6", "company": "kaluste", "mode": "sea", "incoterm": "FCA", "place": "Shanghai",
         "legs": [S("sea", "CNSHA", "DEBRV", carrier="MSC", service="Silk / AE10"),
                  S("sea", "DEBRV", "FIHEL", "feeder", carrier="MSC", service="Baltic feeder"),
                  S("road", "FIHEL", "FILHI", "delivery", to_address=COMPANY_ADDR["kaluste"])],
         "cargo": {"description": "LED furniture lamps and metal fittings (LCL)", "packages": 48, "package_type": "cartons",
                   "weight_kg": 1260, "volume_m3": 9.2, "hs_hint": "9405"},
         "owner": "MSC", "target": ("customs_hold", {"hold": "documents",
                                                       "reason": "Tulli asks for the packing list: the invoice lists 52 cartons, the B/L 48."}),
         "shipper": shipper("Shanghai Brightline Lighting Co., Ltd.", "88 Chuanqiao Road", "201206", "Shanghai", "CN"),
         "refs": {"customer_ref": "PK-2026-0398", "po_numbers": ["PO-77060"]}},
        # ---------------- Nordic Bike Parts Oy (Tampere): bicycle parts
        {"key": "B1", "company": "bike", "mode": "sea", "incoterm": "FOB", "place": "Kaohsiung",
         "legs": [S("sea", "TWKHH", "NLRTM", carrier="Evergreen", service="CEM Asia–North Europe"),
                  S("sea", "NLRTM", "FIHEL", "feeder", carrier="Evergreen", service="Baltic feeder"),
                  S("road", "FIHEL", "FITMP", "delivery", to_address=COMPANY_ADDR["bike"])],
         "cargo": {"description": "Bicycle frames, forks and wheel hubs", "packages": 180, "package_type": "cartons", "weight_kg": 5400,
                   "volume_m3": 54, "containers": [{"type": "40HC", "count": 1}], "hs_hint": "8714"},
         "owner": "EGH", "target": ("customs_hold", {"hold": "customs",
                                                       "reason": "Tulli requests the end-use authorisation for essential bicycle parts (Reg. (EC) 88/97) before release."}),
         "shipper": shipper("Kaohsiung Velo Components Corp.", "No. 21 Zhonghua 5th Rd", "806", "Kaohsiung", "TW"),
         "refs": {"customer_ref": "NBP-5512", "po_numbers": ["4500018812"]}},
        {"key": "B2", "company": "bike", "mode": "air", "incoterm": "FCA", "place": "Taoyuan",
         "legs": [S("air", "TPE", "DOH", carrier="Qatar Airways Cargo", service="Priority, via Doha"),
                  S("air", "DOH", "HEL", "onward", carrier="Qatar Airways Cargo", service="Priority, via Doha"),
                  S("road", "HEL", "FITMP", "delivery", to_address=COMPANY_ADDR["bike"])],
         "cargo": {"description": "Hydraulic disc brake sets (urgent restock)", "packages": 22, "package_type": "cartons", "weight_kg": 386,
                   "volume_m3": 2.1, "hs_hint": "8714"},
         "awb": "157", "target": ("in_transit", {"leg": 2, "f": 0.4}),
         "shipper": shipper("Taichung Brake Systems Ltd.", "No. 9 Gongye 38th Rd", "407", "Taichung", "TW"),
         "refs": {"customer_ref": "NBP-5530", "po_numbers": ["4500018840"]}},
        {"key": "B3", "company": "bike", "mode": "road", "incoterm": "FCA", "place": "Nürnberg",
         "legs": [S("road", "DENUE", "DETRV", "pickup", carrier="DSV", from_address=addr("Frankenstraße 150", "90461", "Nürnberg", "DE")),
                  S("sea", "DETRV", "FIHEL", carrier="Finnlines", service="Ro-ro trailer (unaccompanied)", live_vessel=True),
                  S("road", "FIHEL", "FITMP", "delivery", carrier="Kaukokiito", to_address=COMPANY_ADDR["bike"])],
         "cargo": {"description": "E-bike drive units and batteries (UN3480, Class 9)", "packages": 12, "package_type": "pallets",
                   "weight_kg": 4100, "volume_m3": 14, "ldm": 4.8, "dangerous_goods": True, "dg": {"un": "UN3480", "class": "9"}},
         "target": ("live_vessel", {"leg": 2}),
         "shipper": shipper("Franken eDrive GmbH", "Frankenstraße 150", "90461", "Nürnberg", "DE"),
         "refs": {"customer_ref": "NBP-5524", "po_numbers": ["4500018833"]}},
        {"key": "B4", "company": "bike", "mode": "road", "incoterm": "EXW", "place": "Venlo",
         "legs": [S("road", "NLVEN", "DETRV", "pickup", from_address=addr("Celsiusweg 14", "5928 PR", "Venlo", "NL")),
                  S("sea", "DETRV", "FIHEL", carrier="Finnlines", service="Ro-ro trailer (unaccompanied)"),
                  S("road", "FIHEL", "FITMP", "delivery", to_address=COMPANY_ADDR["bike"])],
         "cargo": {"description": "Bicycle tyres and inner tubes", "packages": 18, "package_type": "pallets", "weight_kg": 5200,
                   "volume_m3": 32, "ldm": 7.2},
         "target": ("booked", {"days": 3.6}), "open_legs": [1, 3],
         "shipper": shipper("Maas Bike Distribution B.V.", "Celsiusweg 14", "5928 PR", "Venlo", "NL"),
         "refs": {"customer_ref": "NBP-5541", "po_numbers": ["4500018861"]}},
        {"key": "B5", "company": "bike", "mode": "sea", "incoterm": "EXW", "place": "Taichung",
         "legs": [S("road", "TWTXG", "TWKHH", "pickup", from_address=addr("No. 66 Jingke Rd", "408", "Taichung", "TW")),
                  S("sea", "TWKHH", "NLRTM", carrier="Evergreen", service="CEM Asia–North Europe"),
                  S("sea", "NLRTM", "FIHEL", "feeder", carrier="Evergreen", service="Baltic feeder"),
                  S("road", "FIHEL", "FITMP", "delivery", to_address=COMPANY_ADDR["bike"])],
         "cargo": {"description": "Derailleurs and chains (LCL)", "packages": 36, "package_type": "cartons", "weight_kg": 980,
                   "volume_m3": 6.4, "hs_hint": "8714"},
         "target": ("picked_up", {"hours": 14}),
         "shipper": shipper("Jingke Drivetrain Co., Ltd.", "No. 66 Jingke Rd", "408", "Taichung", "TW"),
         "refs": {"customer_ref": "NBP-5545", "po_numbers": ["4500018870"]}},
        # ---------------- Saimaan Kahvipaahtimo Oy (Lappeenranta): green coffee
        {"key": "C1", "company": "coffee", "mode": "sea", "incoterm": "FOB", "place": "Santos",
         "legs": [S("sea", "BRSSZ", "DEBRV", carrier="MSC", service="South America–North Europe"),
                  S("sea", "DEBRV", "FIKTK", "feeder", carrier="MSC", service="Baltic feeder"),
                  S("road", "FIKTK", "FILPP", "delivery", partner=True, to_address=COMPANY_ADDR["coffee"])],
         "cargo": {"description": "Green coffee, Brazil Santos NY2 17/18, 320 bags × 60 kg", "packages": 320, "package_type": "bags",
                   "weight_kg": 19520, "volume_m3": 28, "containers": [{"type": "20GP", "count": 1}], "hs_hint": "0901"},
         "owner": "MSC", "target": ("in_transit", {"leg": 1, "f": 0.58}),
         "shipper": shipper("Cooperativa Cafeeira do Sul de Minas", "Rua XV de Novembro 120", "11010-150", "Santos", "BR"),
         "refs": {"customer_ref": "SK-2026-118", "po_numbers": ["SK-PO-3311"]}},
        {"key": "C2", "company": "coffee", "mode": "sea", "incoterm": "FOB", "place": "Cartagena",
         "legs": [S("sea", "COCTG", "DEHAM", carrier="Hapag-Lloyd", service="Caribbean–North Europe"),
                  S("sea", "DEHAM", "FIKTK", "feeder", carrier="Hapag-Lloyd", service="Baltic feeder"),
                  S("road", "FIKTK", "FILPP", "delivery", to_address=COMPANY_ADDR["coffee"])],
         "cargo": {"description": "Green coffee, Colombia Excelso EP, 275 bags × 70 kg", "packages": 275, "package_type": "bags",
                   "weight_kg": 19410, "volume_m3": 27, "containers": [{"type": "20GP", "count": 1}], "hs_hint": "0901"},
         "owner": "HLX", "target": ("at_hub", {"at": "hub"}),
         "shipper": shipper("Café de Altura Exportadora S.A.S.", "Calle 32 #8-21", "130001", "Cartagena", "CO"),
         "refs": {"customer_ref": "SK-2026-121", "po_numbers": ["SK-PO-3315"]}},
        {"key": "C3", "company": "coffee", "mode": "sea", "incoterm": "FOB", "place": "Djibouti",
         "legs": [S("sea", "DJJIB", "BEANR", carrier="CMA CGM", service="Red Sea–North Europe (via Cape)"),
                  S("sea", "BEANR", "FIHEL", "feeder", carrier="CMA CGM", service="Baltic feeder"),
                  S("road", "FIHEL", "FILPP", "delivery", to_address=COMPANY_ADDR["coffee"])],
         "cargo": {"description": "Green coffee, Ethiopia Yirgacheffe Grade 2 (LCL), 60 bags × 60 kg", "packages": 60, "package_type": "bags",
                   "weight_kg": 3660, "volume_m3": 6, "hs_hint": "0901"},
         "target": ("delivered", {"days": 9}),
         "shipper": shipper("Sidama Highland Coffee Exporters PLC", "Bole Road", "1000", "Addis Ababa", "ET"),
         "refs": {"customer_ref": "SK-2026-097", "po_numbers": ["SK-PO-3279"]}},
        {"key": "C4", "company": "coffee", "mode": "sea", "incoterm": "FOB", "place": "Ho Chi Minh City",
         "legs": [S("sea", "VNSGN", "NLRTM", carrier="ONE", service="FE3 Asia–North Europe"),
                  S("sea", "NLRTM", "FIKTK", "feeder", carrier="ONE", service="Baltic feeder"),
                  S("road", "FIKTK", "FILPP", "delivery", to_address=COMPANY_ADDR["coffee"])],
         "cargo": {"description": "Green coffee, Vietnam Robusta Grade 1 S18, 320 bags × 60 kg", "packages": 320, "package_type": "bags",
                   "weight_kg": 19500, "volume_m3": 28, "containers": [{"type": "20GP", "count": 1}], "hs_hint": "0901"},
         "owner": "ONE", "target": ("exception", {"at": "hub", "delay_days": 5,
                                                    "note": "Container rolled at Rotterdam: feeder fully booked. Re-booked on the next sailing, ETA +5 days."}),
         "shipper": shipper("Dak Lak Robusta Trading Co.", "12 Le Duan", "630000", "Buon Ma Thuot", "VN"),
         "refs": {"customer_ref": "SK-2026-109", "po_numbers": ["SK-PO-3298"]}},
        {"key": "C5", "company": "coffee", "mode": "road", "incoterm": "FCA", "place": "Hamburg",
         "legs": [S("road", "DEHAM", "DETRV", "pickup", carrier="DSV", from_address=addr("Am Sandtorkai 23", "20457", "Hamburg", "DE")),
                  S("sea", "DETRV", "FIHEL", carrier="Finnlines", service="Ro-ro trailer (unaccompanied)"),
                  S("road", "FIHEL", "FILPP", "delivery", partner=True, to_address=COMPANY_ADDR["coffee"])],
         "cargo": {"description": "Specialty green coffee from bonded warehouse, mixed origins", "packages": 10, "package_type": "pallets",
                   "weight_kg": 6200, "volume_m3": 12, "ldm": 4.0, "hs_hint": "0901"},
         "target": ("out_for_delivery", {"f": 0.45}),
         "shipper": shipper("Speicherstadt Green Coffee Traders GmbH", "Am Sandtorkai 23", "20457", "Hamburg", "DE"),
         "refs": {"customer_ref": "SK-2026-124", "po_numbers": ["SK-PO-3320"]}},
        # ---------------- Arktinen Elektroniikka Oy (Oulu): electronics
        {"key": "E1", "company": "electro", "mode": "air", "incoterm": "FCA", "place": "Hong Kong",
         "legs": [S("air", "HKG", "HEL", carrier="Finnair Cargo", service="Direct"),
                  S("road", "HEL", "FIOUL", "delivery", to_address=COMPANY_ADDR["electro"])],
         "cargo": {"description": "5G radio modules and RF test boards", "packages": 18, "package_type": "cartons", "weight_kg": 412,
                   "volume_m3": 2.6, "hs_hint": "8517"},
         "awb": "105", "target": ("customs_hold", {"hold": "inspection",
                                                    "reason": "Tulli physical inspection: radio equipment conformity (RED 2014/53/EU) check."}),
         "shipper": shipper("Kowloon RF Devices Ltd.", "8 Science Park Ave", "999077", "Hong Kong", "HK"),
         "refs": {"customer_ref": "AE-PO-88412", "po_numbers": ["88412"]}},
        {"key": "E2", "company": "electro", "mode": "air", "incoterm": "DAP", "place": "Oulu",
         "legs": [S("air", "ORD", "HEL", carrier="DHL Global Forwarding", service="Air Connect", transport_doc_no="6731988420"),
                  S("road", "HEL", "FIOUL", "delivery", carrier="Posti Freight", to_address=COMPANY_ADDR["electro"])],
         "cargo": {"description": "FPGA evaluation kits", "packages": 3, "package_type": "boxes", "weight_kg": 21, "volume_m3": 0.12,
                   "hs_hint": "8471"},
         "target": ("in_transit", {"leg": 1, "f": 0.62}),
         "shipper": shipper("Midwest Embedded Systems Inc.", "200 W Monroe St", "60606", "Chicago", "US"),
         "refs": {"customer_ref": "AE-PO-88431", "po_numbers": ["88431"]}},
        {"key": "E3", "company": "electro", "mode": "sea", "incoterm": "FOB", "place": "Busan",
         "legs": [S("sea", "KRPUS", "NLRTM", carrier="ONE", service="FP1 Asia–North Europe"),
                  S("sea", "NLRTM", "FIOUL", "feeder", carrier="ONE", service="Gulf of Bothnia feeder"),
                  S("road", "FIOUL", "FIOUL", "delivery", to_address=COMPANY_ADDR["electro"])],
         "cargo": {"description": "Server racks and power distribution units", "packages": 22, "package_type": "crates", "weight_kg": 7800,
                   "volume_m3": 52, "containers": [{"type": "40HC", "count": 1}], "hs_hint": "8471"},
         "owner": "ONE", "target": ("booked", {"days": 4.5}), "open_legs": [3],
         "shipper": shipper("Busan Precision Enclosures Co.", "33 Sinhang-ro", "46762", "Busan", "KR"),
         "refs": {"customer_ref": "AE-PO-88450", "po_numbers": ["88450"]}},
        {"key": "E4", "company": "electro", "mode": "rail", "incoterm": "DAP", "place": "Oulu",
         "legs": [S("rail", "FIHEL", "FIOUL", carrier="VR Transpoint", service="Container train Vuosaari–Oulu")],
         "cargo": {"description": "Transshipped 40HC: PCB assembly line equipment", "packages": 9, "package_type": "crates", "weight_kg": 11800,
                   "volume_m3": 61, "containers": [{"type": "40HC", "count": 1}], "hs_hint": "8479"},
         "owner": "TGH", "target": ("in_transit", {"leg": 1, "f": 0.35}),
         "shipper": shipper("Huolitsija Vuosaari terminal (transshipment)", "Vuosaaren satama", "00980", "Helsinki", "FI"),
         "refs": {"customer_ref": "AE-PO-88395", "po_numbers": ["88395"]}},
        {"key": "E5", "company": "electro", "mode": "road", "incoterm": "FCA", "place": "Stuttgart",
         "legs": [S("road", "DESTR", "DETRV", "pickup", carrier="DSV", from_address=addr("Industriestraße 40", "70565", "Stuttgart", "DE")),
                  S("sea", "DETRV", "FIHEL", carrier="Finnlines", service="Ro-ro trailer (unaccompanied)"),
                  S("road", "FIHEL", "FIOUL", "delivery", carrier="Kaukokiito", to_address=COMPANY_ADDR["electro"])],
         "cargo": {"description": "SMT pick-and-place machine", "packages": 2, "package_type": "crates", "weight_kg": 3900,
                   "volume_m3": 18, "ldm": 4.4, "hs_hint": "8479"},
         "target": ("delivered", {"days": 3}),
         "shipper": shipper("Schwaben Fertigungstechnik GmbH", "Industriestraße 40", "70565", "Stuttgart", "DE"),
         "refs": {"customer_ref": "AE-PO-88360", "po_numbers": ["88360"]}},
        # ---------------- Kymen Teräskauppa Oy (Kotka): steel
        {"key": "S1", "company": "steel", "mode": "rail", "incoterm": "DAP", "place": None, "live_train": True,
         "legs": [S("rail", "FIKVO", "FIOUL", carrier="VR Transpoint", service="Wagonload")],
         "cargo": {"description": "Steel hollow sections, 1 wagon", "packages": 32, "package_type": "bundles", "weight_kg": 36800,
                   "volume_m3": None, "hs_hint": "7208"},
         "target": ("live_train", {}),
         "shipper": shipper("Kymen Teräskauppa Oy, Mussalo stock", "Mussalontie 428", "48310", "Kotka", "FI"),
         "refs": {"customer_ref": "KT-RAIL-2210", "po_numbers": []}},
        {"key": "S2", "company": "steel", "mode": "sea", "incoterm": "CFR", "place": "Kotka",
         "legs": [S("sea", "PLGDN", "FIKTK", carrier="Maersk", service="Baltic feeder", live_vessel=True),
                  S("road", "FIKTK", "FIKTK", "delivery", to_address=COMPANY_ADDR["steel"])],
         "cargo": {"description": "Cold-rolled steel sheet in coils", "packages": 6, "package_type": "coils", "weight_kg": 26400,
                   "volume_m3": None, "containers": [{"type": "20GP", "count": 1}], "hs_hint": "7209"},
         "owner": "MSK", "target": ("live_vessel", {"leg": 1}),
         "shipper": shipper("Pomorskie Steel Service Center S.A.", "ul. Przemysłowa 8", "80-542", "Gdańsk", "PL"),
         "refs": {"customer_ref": "KT-2026-0915", "po_numbers": ["KT-4410"]}},
        {"key": "S3", "company": "steel", "mode": "road", "incoterm": "FCA", "place": "Luleå",
         "legs": [S("road", "SELLA", "FIKTK", carrier="Girteka", service="FTL via Haparanda–Tornio",
                    from_address=addr("Hamnleden 2", "974 37", "Luleå", "SE"), to_address=COMPANY_ADDR["steel"])],
         "cargo": {"description": "Wear-resistant steel plate", "packages": 8, "package_type": "bundles", "weight_kg": 24000,
                   "volume_m3": None, "ldm": 13.6, "hs_hint": "7208"},
         "target": ("in_transit", {"leg": 1, "f": 0.3}),
         "shipper": shipper("Norrbotten Plate & Profiles AB", "Hamnleden 2", "974 37", "Luleå", "SE"),
         "refs": {"customer_ref": "KT-2026-0921", "po_numbers": ["KT-4415"]}},
        {"key": "S4", "company": "steel", "mode": "road", "incoterm": "DAP", "place": "Stockholm", "direction": "export",
         "legs": [S("road", "FIKTK", "FIHEL", "pickup", carrier="Girteka", service="FTL", from_address=COMPANY_ADDR["steel"]),
                  S("sea", "FIHEL", "SESTO", carrier="Tallink Grupp", service="Ro-pax Helsinki–Stockholm"),
                  S("road", "SESTO", "SESTO", "delivery", carrier="Girteka", to_address=addr("Lindhagensgatan 112", "112 51", "Stockholm", "SE"))],
         "cargo": {"description": "Galvanised steel profiles", "packages": 18, "package_type": "bundles", "weight_kg": 21500,
                   "volume_m3": None, "ldm": 13.6, "hs_hint": "7216"},
         "target": ("booked", {"days": 1.4}),
         "consignee": {"name": "Mälardalens Byggstål AB", "address": addr("Lindhagensgatan 112", "112 51", "Stockholm", "SE")},
         "shipper": {"name": COMPANY_NAME["steel"], "address": COMPANY_ADDR["steel"]},
         "refs": {"customer_ref": "KT-EXP-0077", "po_numbers": ["MB-55821"]}},
        {"key": "S5", "company": "steel", "mode": "multimodal", "incoterm": "FCA", "place": "Duisburg",
         "legs": [S("rail", "DEDUI", "DEHAM", carrier="DB Cargo", service="Block train Duisburg–Hamburg"),
                  S("sea", "DEHAM", "FIKTK", "feeder", carrier="Hapag-Lloyd", service="Baltic feeder"),
                  S("road", "FIKTK", "FIKTK", "delivery", to_address=COMPANY_ADDR["steel"])],
         "cargo": {"description": "Steel wire rod in coils", "packages": 12, "package_type": "coils", "weight_kg": 24000,
                   "volume_m3": None, "containers": [{"type": "20GP", "count": 1}], "hs_hint": "7213"},
         "owner": "HLX", "target": ("delivered", {"days": 4}),
         "shipper": shipper("Ruhr Drahtwerk GmbH", "Hafenstraße 31", "47119", "Duisburg", "DE"),
         "refs": {"customer_ref": "KT-2026-0880", "po_numbers": ["KT-4377"]}},
        # ---------------- Lapin Retkivaruste Oy (Rovaniemi): outdoor gear
        {"key": "O1", "company": "outdoor", "mode": "rail", "incoterm": "FCA", "place": "Xi'an",
         "legs": [S("rail", "CNXIA", "KZAAU", carrier="Rail Cargo Group", service="Middle Corridor block train (TITR)"),
                  S("sea", "KZAAU", "AZALA", "ferry", carrier="ASCO (Azerbaijan Caspian Shipping)", service="Caspian rail ferry Aktau–Alat"),
                  S("rail", "AZALA", "DEDUI", carrier="Rail Cargo Group", service="Middle Corridor block train (TITR)"),
                  S("road", "DEDUI", "DETRV", "onward", carrier="DSV"),
                  S("sea", "DETRV", "FIHEL", "onward", carrier="Finnlines", service="Ro-ro trailer (unaccompanied)"),
                  S("road", "FIHEL", "FIRVN", "delivery", carrier="Kaukokiito", to_address=COMPANY_ADDR["outdoor"])],
         "cargo": {"description": "Tents, sleeping bags and trekking poles", "packages": 640, "package_type": "cartons", "weight_kg": 8900,
                   "volume_m3": 67, "containers": [{"type": "40HC", "count": 1}], "hs_hint": "6306"},
         "owner": "TGH", "target": ("in_transit", {"leg": 1, "f": 0.34}),
         "shipper": shipper("Shaanxi Summit Outdoor Equipment Co.", "Gangwu Avenue 9", "710026", "Xi'an", "CN"),
         "refs": {"customer_ref": "LR-AW26-014", "po_numbers": ["LR-1180"]}},
        {"key": "O2", "company": "outdoor", "mode": "sea", "incoterm": "FOB", "place": "Hai Phong",
         "legs": [S("sea", "VNHPH", "NLRTM", carrier="CMA CGM", service="FAL1 French Asia Line"),
                  S("sea", "NLRTM", "FIHEL", "feeder", carrier="CMA CGM", service="Baltic feeder"),
                  S("road", "FIHEL", "FIRVN", "delivery", to_address=COMPANY_ADDR["outdoor"])],
         "cargo": {"description": "Down jackets and softshell trousers", "packages": 520, "package_type": "cartons", "weight_kg": 6100,
                   "volume_m3": 64, "containers": [{"type": "40HC", "count": 1}], "hs_hint": "6201"},
         "owner": "CMA", "target": ("in_transit", {"leg": 1, "f": 0.21}),
         "shipper": shipper("Haiphong Alpine Garment JSC", "Lot B2 Nomura IZ", "180000", "Hai Phong", "VN"),
         "refs": {"customer_ref": "LR-AW26-009", "po_numbers": ["LR-1172", "LR-1173"]}},
        {"key": "O3", "company": "outdoor", "mode": "road", "incoterm": "FCA", "place": "Umeå",
         "legs": [S("road", "SEUME", "FIRVN", carrier="Girteka", service="FTL via Haparanda–Tornio",
                    from_address=addr("Formvägen 5", "906 21", "Umeå", "SE"), to_address=COMPANY_ADDR["outdoor"])],
         "cargo": {"description": "Wool base layers and hiking socks", "packages": 9, "package_type": "pallets", "weight_kg": 2300,
                   "volume_m3": 19, "ldm": 3.6, "hs_hint": "6109"},
         "target": ("delivered", {"days": 2}),
         "shipper": shipper("Västerbotten Wool AB", "Formvägen 5", "906 21", "Umeå", "SE"),
         "refs": {"customer_ref": "LR-AW26-002", "po_numbers": ["LR-1150"]}},
        {"key": "O4", "company": "outdoor", "mode": "road", "incoterm": "FCA", "place": "Oslo",
         "legs": [S("road", "NOOSL", "FIRVN", carrier="Girteka", service="FTL via Haparanda–Tornio",
                    from_address=addr("Brynsveien 3", "0667", "Oslo", "NO"), to_address=COMPANY_ADDR["outdoor"])],
         "cargo": {"description": "Ski touring bindings and climbing skins", "packages": 6, "package_type": "pallets", "weight_kg": 1500,
                   "volume_m3": 10, "ldm": 2.4, "hs_hint": "9506"},
         "target": ("booked", {"days": 1.8}),
         "shipper": shipper("Nordmarka Ski Equipment AS", "Brynsveien 3", "0667", "Oslo", "NO"),
         "refs": {"customer_ref": "LR-AW26-021", "po_numbers": ["LR-1195"]}},
        {"key": "O5", "company": "outdoor", "mode": "road", "incoterm": "FCA", "place": "Montebelluna",
         "legs": [S("road", "ITVRN", "DETRV", "pickup", carrier="DSV", from_address=addr("Via Feltrina 210", "31044", "Montebelluna", "IT")),
                  S("sea", "DETRV", "FIHEL", carrier="Finnlines", service="Ro-ro trailer (unaccompanied)"),
                  S("road", "FIHEL", "FIRVN", "delivery", carrier="Kaukokiito", to_address=COMPANY_ADDR["outdoor"])],
         "cargo": {"description": "Ski boots and hiking boots", "packages": 14, "package_type": "pallets", "weight_kg": 3100,
                   "volume_m3": 26, "ldm": 5.6, "hs_hint": "6403"},
         "target": ("exception", {"at": "before_leg", "leg": 2, "delay_days": 2,
                                  "note": "Two pallets damaged at Travemünde terminal (forklift). Re-wrapped and photographed; trailer re-booked on the next Finnlines sailing."}),
         "shipper": shipper("Calzaturificio Montello S.r.l.", "Via Feltrina 210", "31044", "Montebelluna", "IT"),
         "refs": {"customer_ref": "LR-AW26-011", "po_numbers": ["LR-1176"]}},
        # ---------------- Turun Venetarvike Oy (Turku): marine supplies
        {"key": "M1", "company": "marine", "mode": "sea", "incoterm": "FOB", "place": "Savannah",
         "legs": [S("sea", "USSAV", "DEHAM", carrier="Hapag-Lloyd", service="Atlantic Loop AL5"),
                  S("sea", "DEHAM", "FIRAU", "feeder", carrier="Hapag-Lloyd", service="Baltic feeder"),
                  S("road", "FIRAU", "FITKU", "delivery", to_address=COMPANY_ADDR["marine"])],
         "cargo": {"description": "Outboard motors 40–150 hp", "packages": 34, "package_type": "crates", "weight_kg": 6800,
                   "volume_m3": 48, "containers": [{"type": "40GP", "count": 1}], "hs_hint": "8407"},
         "owner": "HLX", "target": ("in_transit", {"leg": 1, "f": 0.66}),
         "shipper": shipper("Coastal Marine Engines LLC", "150 Brampton Rd", "31408", "Savannah", "US"),
         "refs": {"customer_ref": "TV-2026-302", "po_numbers": ["TV-9031"]}},
        {"key": "M2", "company": "marine", "mode": "road", "incoterm": "FCA", "place": "Milano",
         "legs": [S("road", "ITMIL", "DELBC", "pickup", carrier="DSV", from_address=addr("Via Mecenate 76", "20138", "Milano", "IT")),
                  S("sea", "DELBC", "FIHKO", carrier="Finnlines", service="Ro-ro trailer (unaccompanied)", live_vessel=True),
                  S("road", "FIHKO", "FITKU", "delivery", carrier="Kaukokiito", to_address=COMPANY_ADDR["marine"])],
         "cargo": {"description": "Teak deck fittings and stainless hardware", "packages": 8, "package_type": "pallets", "weight_kg": 2600,
                   "volume_m3": 11, "ldm": 3.2, "hs_hint": "7326"},
         "target": ("live_vessel", {"leg": 2}),
         "shipper": shipper("Nautica Lombarda S.p.A.", "Via Mecenate 76", "20138", "Milano", "IT"),
         "refs": {"customer_ref": "TV-2026-311", "po_numbers": ["TV-9040"]}},
        {"key": "M3", "company": "marine", "mode": "road", "incoterm": "DAP", "place": "Stockholm", "direction": "export",
         "legs": [S("road", "FITKU", "FITKU", "pickup", carrier="PostNord", service="Groupage", from_address=COMPANY_ADDR["marine"]),
                  S("sea", "FITKU", "SESTO", carrier="Viking Line", service="Ro-pax Turku–Stockholm"),
                  S("road", "SESTO", "SESTO", "delivery", carrier="PostNord", to_address=addr("Strandvägen 7", "114 56", "Stockholm", "SE"))],
         "cargo": {"description": "Marine electronics and chartplotters (export)", "packages": 4, "package_type": "pallets", "weight_kg": 640,
                   "volume_m3": 4.5, "ldm": 1.6, "hs_hint": "8526"},
         "target": ("delivered", {"days": 5}),
         "consignee": {"name": "Skärgårdens Båttillbehör AB", "address": addr("Strandvägen 7", "114 56", "Stockholm", "SE")},
         "shipper": {"name": COMPANY_NAME["marine"], "address": COMPANY_ADDR["marine"]},
         "refs": {"customer_ref": "TV-EXP-118", "po_numbers": ["SB-2207"]}},
        {"key": "M4", "company": "marine", "mode": "air", "incoterm": "FCA", "place": "New York",
         "legs": [S("air", "JFK", "HEL", carrier="Finnair Cargo", service="Direct"),
                  S("road", "HEL", "FITKU", "delivery", to_address=COMPANY_ADDR["marine"])],
         "cargo": {"description": "Autopilot drive units (AOG spare for charter fleet)", "packages": 5, "package_type": "cartons",
                   "weight_kg": 140, "volume_m3": 0.6, "hs_hint": "9014"},
         "awb": "105", "target": ("at_hub", {"at": "pod"}),
         "shipper": shipper("Atlantic Nav Systems Inc.", "55 Water St", "10041", "New York", "US"),
         "refs": {"customer_ref": "TV-2026-320", "po_numbers": ["TV-9052"]}},
        {"key": "M5", "company": "marine", "mode": "sea", "incoterm": "EXW", "place": "Göteborg",
         "legs": [S("road", "SEGOT", "SEGOT", "pickup", from_address=addr("Gropegårdsgatan 2", "417 15", "Göteborg", "SE")),
                  S("sea", "SEGOT", "FIHEL", carrier="Unifeeder", service="North Sea–Baltic feeder"),
                  S("road", "FIHEL", "FITKU", "delivery", to_address=COMPANY_ADDR["marine"])],
         "cargo": {"description": "Marine diesel engine spare parts", "packages": 6, "package_type": "pallets", "weight_kg": 2900,
                   "volume_m3": 9, "hs_hint": "8409"},
         "target": ("picked_up", {"hours": 9}),
         "shipper": shipper("Västkustens Marinmotor AB", "Gropegårdsgatan 2", "417 15", "Göteborg", "SE"),
         "refs": {"customer_ref": "TV-2026-326", "po_numbers": ["TV-9060"]}},
        # ---------------- Helsinki Design House Oy (Helsinki): fashion
        {"key": "F1", "company": "fashion", "mode": "road", "incoterm": "FCA", "place": "Prato",
         "legs": [S("road", "ITPRO", "DETRV", "pickup", carrier="DSV", from_address=addr("Via Galcianese 52", "59100", "Prato", "IT")),
                  S("sea", "DETRV", "FIHEL", carrier="Finnlines", service="Ro-ro trailer (unaccompanied)"),
                  S("road", "FIHEL", "FIHEL", "delivery", partner=True, to_address=COMPANY_ADDR["fashion"])],
         "cargo": {"description": "Wool and cashmere fabrics for AW27 collection", "packages": 7, "package_type": "pallets", "weight_kg": 1900,
                   "volume_m3": 9, "ldm": 2.8, "hs_hint": "5112"},
         "target": ("out_for_delivery", {"f": 0.3}),
         "shipper": shipper("Lanificio Bisenzio S.r.l.", "Via Galcianese 52", "59100", "Prato", "IT"),
         "refs": {"customer_ref": "HDH-AW27-03", "po_numbers": ["HDH-6603"]}},
        {"key": "F2", "company": "fashion", "mode": "road", "incoterm": "FCA", "place": "Porto",
         "legs": [S("road", "PTOPO", "DETRV", "pickup", partner=True, from_address=addr("Rua do Freixo 1071", "4300-219", "Porto", "PT")),
                  S("sea", "DETRV", "FIHEL", carrier="Finnlines", service="Ro-ro trailer (unaccompanied)"),
                  S("road", "FIHEL", "FIHEL", "delivery", partner=True, to_address=COMPANY_ADDR["fashion"])],
         "cargo": {"description": "Leather shoes and bags, SS27 samples and first production", "packages": 5, "package_type": "pallets",
                   "weight_kg": 1100, "volume_m3": 7, "ldm": 2.0, "hs_hint": "6403"},
         "target": ("booked", {"days": 2.9}),
         "shipper": shipper("Norte Couro Calçado Lda.", "Rua do Freixo 1071", "4300-219", "Porto", "PT"),
         "refs": {"customer_ref": "HDH-SS27-01", "po_numbers": ["HDH-6611"]}},
        {"key": "F3", "company": "fashion", "mode": "sea", "incoterm": "FOB", "place": "Chittagong",
         "legs": [S("sea", "BDCGP", "DEBRV", carrier="MSC", service="Indian Subcontinent–North Europe"),
                  S("sea", "DEBRV", "FIHEL", "feeder", carrier="MSC", service="Baltic feeder"),
                  S("road", "FIHEL", "FIHEL", "delivery", partner=True, to_address=COMPANY_ADDR["fashion"])],
         "cargo": {"description": "Organic cotton T-shirts and hoodies", "packages": 860, "package_type": "cartons", "weight_kg": 9200,
                   "volume_m3": 66, "containers": [{"type": "40HC", "count": 1}], "hs_hint": "6109"},
         "owner": "MSC", "target": ("in_transit", {"leg": 1, "f": 0.71}),
         "shipper": shipper("Chattogram Knit Composite Ltd.", "Plot 14, CEPZ", "4223", "Chattogram", "BD"),
         "refs": {"customer_ref": "HDH-AW26-12", "po_numbers": ["HDH-6540"]}},
        {"key": "F4", "company": "fashion", "mode": "air", "incoterm": "FCA", "place": "Shanghai",
         "legs": [S("air", "PVG", "HEL", carrier="Finnair Cargo", service="Direct"),
                  S("road", "HEL", "FIHEL", "delivery", partner=True, to_address=COMPANY_ADDR["fashion"])],
         "cargo": {"description": "Silk scarves and printed blouses", "packages": 24, "package_type": "cartons", "weight_kg": 310,
                   "volume_m3": 2.4, "hs_hint": "6214"},
         "awb": "105", "target": ("delivered", {"days": 1}),
         "shipper": shipper("Suzhou Silk Atelier Co., Ltd.", "199 Renmin Road", "215000", "Suzhou", "CN"),
         "refs": {"customer_ref": "HDH-AW26-15", "po_numbers": ["HDH-6555"]}},
        {"key": "F5", "company": "fashion", "mode": "road", "incoterm": "FCA", "place": "İstanbul",
         "legs": [S("road", "TRIST", "EEMUG", "pickup", carrier="Girteka", service="FTL, T1 transit via Kapıkule and Via Baltica",
                    from_address=addr("Merter Keresteciler Sitesi 4", "34169", "İstanbul", "TR")),
                  S("sea", "EEMUG", "FIHEL", carrier="Eckerö Line (Finbo Cargo)", service="Ro-pax, accompanied trailer Muuga–Vuosaari"),
                  S("road", "FIHEL", "FIHEL", "delivery", carrier="Girteka", to_address=COMPANY_ADDR["fashion"])],
         "cargo": {"description": "Denim jeans and jackets", "packages": 22, "package_type": "pallets", "weight_kg": 6400,
                   "volume_m3": 58, "ldm": 13.6, "hs_hint": "6203"},
         "target": ("exception", {"at": "leg", "leg": 1, "f": 0.12, "delay_days": 2,
                                  "note": "Truck queued at the Kapıkule border crossing (Türkiye–Bulgaria) for 40 h; transit (T1) accepted, delivery +2 days."}),
         "shipper": shipper("Bosphorus Denim Tekstil A.Ş.", "Merter Keresteciler Sitesi 4", "34169", "İstanbul", "TR"),
         "refs": {"customer_ref": "HDH-AW26-18", "po_numbers": ["HDH-6570"]}},
    ]


# --------------------------------------------------------------------------------------------- building

def shift_schedule(legs: list[dict], ms: list[dict], delta: timedelta):
    for lg in legs:
        for k in ("etd", "eta"):
            if lg.get(k):
                lg[k] = iso(parse_iso(lg[k]) + delta)
    for m in ms:
        if m.get("planned"):
            m["planned"] = iso(parse_iso(m["planned"]) + delta)


def first(ms, code, leg_seq=None):
    return next((m for m in ms if m["code"] == code and (leg_seq is None or m.get("leg_seq") == leg_seq)), None)


def fill_carriers(leg_specs: list[dict], spec: dict):
    """Every leg gets the haulier a forwarder would really use, except legs the spec leaves open (`open_legs`)."""
    open_legs = set(spec.get("open_legs") or [])
    fcl = bool(spec["cargo"].get("containers"))
    for i, ls in enumerate(leg_specs, start=1):
        if ls.get("carrier") or ls.get("partner") or i in open_legs:
            continue
        nxt = leg_specs[i] if i < len(leg_specs) else None
        prv = leg_specs[i - 2] if i > 1 else None
        if ls["mode"] == "road" and ls.get("role") == "pickup" and nxt and nxt["mode"] in ("sea", "rail") and nxt.get("carrier"):
            ls["carrier"], ls["service"] = nxt["carrier"], ls.get("service") or "Carrier haulage"
        elif ls["mode"] == "road" and ls.get("role") == "pickup" and nxt and nxt["mode"] == "air":
            ls["carrier"], ls["service"] = "DHL Global Forwarding", ls.get("service") or "Origin pickup (agent)"
        elif ls["mode"] == "road" and ls.get("role") == "delivery" and prv and prv["mode"] == "air":
            ls["carrier"] = "Posti Freight"
        elif ls["mode"] == "road":
            ls["carrier"] = "Containerships (CMA CGM Group)" if fcl else "Kaukokiito"
        elif ls["mode"] == "sea" and prv and prv.get("carrier") and prv["mode"] == "sea":
            ls["carrier"] = prv["carrier"]


async def build_one(spec: dict, vessels: dict, used: set, train: dict | None, rnd: random.Random, partner: dict,
                    keep: dict | None = None) -> dict | None:
    company = await db.companies.find_one({"id": demo_company_id(spec["company"])}, NO_ID)
    if not company:
        return None
    leg_specs = [dict(x) for x in spec["legs"]]
    if spec["mode"] == "road" and leg_specs[0].get("role") == "pickup" and len(leg_specs) > 1:
        leg_specs[0]["role"] = "main"  # road product: the collecting truck is the main carriage (see services.shipments.road_specs)
    fill_carriers(leg_specs, spec)
    for ls in leg_specs:
        if ls.get("carrier"):
            ls["carrier_id"], ls["carrier"] = await carrier_ref(ls["carrier"])
        if ls.pop("partner", False):
            ls["carrier_id"], ls["carrier"] = partner["id"], partner["name"]
    target, p = spec["target"]
    vessel = None
    if target == "live_vessel":
        ls = leg_specs[p["leg"] - 1]
        roro = "ro-ro" in (ls.get("service") or "").lower()
        to_code = ls["to"] if isinstance(ls["to"], str) else ls["to"].get("code")
        frm_code = ls["from"] if isinstance(ls["from"], str) else ""
        vessel = pick_vessel(vessels, to_code, prev_prefer=[frm_code, "DETRV", "DELBC", "DERSK", "DEHAM", "DEBRV", "NLRTM", "PLGDN", "PLGDY",
                                                            "LTKLJ", "LVRIX", "SEGOT", "EETLL", "EEMUG"],
                             types={20, 40, 70} if roro else {50, 70}, used=used, max_hours=30 if roro else 96)
        if vessel:
            ls["vessel"] = {"name": vessel["name"], "imo": vessel["imo"], "mmsi": vessel["mmsi"]}
            prev = (vessel.get("prev") or "").upper()
            if prev and prev != frm_code and geo.lookup_code(prev) and prev[:2] != "FI":
                prev_leg = leg_specs[p["leg"] - 2] if p["leg"] > 1 else None
                if prev in HUBS and prev_leg and prev_leg["mode"] == "sea":
                    prev_leg["to"] = prev  # the feeder's real previous port becomes the transshipment hub
                    ls["from"] = prev
                elif not roro:
                    ls["via"] = [prev]
        else:
            target, p = "in_transit", {"leg": p["leg"], "f": 0.6}
    if target == "live_train" and train:
        ls = leg_specs[0]
        ls["from"], ls["to"] = train["from"], train["to"]
        ls["train"] = {"number": train["number"], "departure_date": train["departure_date"], "operator": train["operator"]}
        ls["service"] = f"Cargo train {train['number']} ({train['operator'] or 'rail operator'})"
    elif target == "live_train":
        target, p = "in_transit", {"leg": 1, "f": 0.4}

    legs = await svc.build_legs(leg_specs)
    if target == "live_train" and train:
        legs[0]["path"] = geo.densify(geo.simplify(train["path"]))
        legs[0]["path_source"] = "rail_corridor"
        legs[0]["path_note"] = "Station sequence of the train's own timetable (rata.digitraffic.fi)"
        legs[0]["distance_km"] = geo.path_length_km(legs[0]["path"])
    direction = spec.get("direction") or svc.infer_direction(legs[0]["from"], legs[-1]["to"])
    t0 = now()
    svc.schedule_legs(legs, t0)
    customs = direction in ("import", "export") and not all(
        (lg["from"].get("country") in svc.EU_EEA - {"NO", "IS", "LI"}) and (lg["to"].get("country") in svc.EU_EEA - {"NO", "IS", "LI"})
        for lg in legs)
    ms = svc.planned_milestones(legs, direction, customs=customs)

    # ---- anchor the schedule so that "now" falls where the target status says
    if target == "booked":
        delta = timedelta(days=p["days"])
    elif target == "picked_up":
        rec = first(ms, "received")
        delta = t0 - (parse_iso(rec["planned"]) + timedelta(hours=p["hours"]))
    elif target in ("in_transit", "exception") and p.get("leg") and p.get("at") in (None, "leg"):
        lg = legs[p["leg"] - 1]
        a, b = parse_iso(lg["etd"]), parse_iso(lg["eta"])
        delta = -((a + (b - a) * p["f"]) - t0)
    elif target == "exception" and p.get("at") == "before_leg":
        lg = legs[p["leg"] - 1]
        delta = t0 - (parse_iso(lg["etd"]) - timedelta(hours=3))
    elif target in ("at_hub", "exception") and p.get("at") == "hub":
        tm = first(ms, "transshipment")
        delta = -((parse_iso(tm["planned"]) + timedelta(hours=30)) - t0)
    elif target == "at_hub" and p.get("at") == "pod":
        ar = first(ms, "arrived")
        delta = -((parse_iso(ar["planned"]) + timedelta(hours=8)) - t0)
    elif target == "customs_hold":
        ar = first(ms, "arrived")
        delta = -((parse_iso(ar["planned"]) + timedelta(hours=26)) - t0)
    elif target == "out_for_delivery":
        dl = next(lg for lg in legs if lg["role"] == "delivery")
        a, b = parse_iso(dl["etd"]), parse_iso(dl["eta"])
        delta = -((a + (b - a) * p["f"]) - t0)
    elif target == "delivered":
        delta = -((parse_iso(legs[-1]["eta"]) + timedelta(days=p["days"])) - t0)
    elif target == "live_vessel":
        lg = legs[p["leg"] - 1]
        delta = parse_iso(vessel["eta"]) - parse_iso(lg["eta"])
    elif target == "live_train":
        delta = parse_iso(train["start"]) - parse_iso(legs[0]["etd"])
    else:
        delta = timedelta(0)
    shift_schedule(legs, ms, delta)
    if target == "live_train" and train:
        legs[0]["eta"] = train["end"]
        for m in ms:
            if m.get("leg_seq") == 1 and m["code"] in ("arrived", "delivered"):
                m["planned"] = train["end"]
    if target == "live_vessel":
        lg = legs[p["leg"] - 1]
        lg["eta"] = vessel["eta"]
        lg["port_call"] = {"port_call_id": vessel.get("port_call_id"), "port_area": vessel.get("area")}

    # ---- references: containers, AWB, booking numbers
    refs = {"customer_ref": None, "po_numbers": [], **spec.get("refs", {})}
    conts = []
    for c in spec["cargo"].get("containers") or []:
        for _ in range(int(c.get("count") or 1)):
            conts.append(container_no(spec.get("owner") or "TGH", rnd))
    if conts:
        refs["container_numbers"] = conts
        refs["booking_no"] = f"{(spec.get('owner') or 'BK')[:3]}{rnd.randint(10_000_000, 99_999_999)}"
        for lg in legs:
            if lg["mode"] in ("sea", "rail"):
                lg["container_numbers"] = conts
    if spec.get("awb"):
        refs["awb_no"] = awb_no(spec["awb"], rnd)
        for lg in legs:
            if lg["mode"] == "air":
                lg["transport_doc_no"] = refs["awb_no"]
    for lg in legs:
        if lg["mode"] == "road" and lg["role"] in ("main", "delivery", "pickup") and not lg.get("transport_doc_no") and lg["from"].get("country") != lg["to"].get("country"):
            lg["transport_doc_no"] = f"CMR {rnd.randint(100000, 999999)}"
        if lg["mode"] == "road" and lg.get("carrier") and not lg.get("truck_plate") and target not in ("booked",):
            lg["truck_plate"] = f"{rnd.choice(['ZXV', 'KTA', 'OUL', 'LHT', 'TKU', 'RVN'])}-{rnd.randint(100, 999)}"

    consignee_party = spec.get("consignee") or consignee(spec["company"], {"kaluste": "Mikko Laine", "bike": "Sanna Virtanen",
                                                                            "coffee": "Jari Heikkinen", "electro": "Anna Korhonen",
                                                                            "steel": "Petri Mäkelä", "outdoor": "Laura Niemi",
                                                                            "marine": "Tomi Salonen", "fashion": "Emma Lehtinen"}[spec["company"]])
    s = await svc._assemble(
        company=company, mode=spec["mode"], direction=direction, incoterm=spec.get("incoterm"), incoterm_place=spec.get("place"),
        origin=dict(legs[0]["from"]), destination=dict(legs[-1]["to"]), legs=legs, etd=parse_iso(legs[0]["etd"]),
        eta=parse_iso(legs[-1]["eta"]), cargo=dict(spec["cargo"]),
        parties={"shipper": spec.get("shipper"), "consignee": consignee_party, "notify": None}, references=refs,
        customs=customs, insurance={"type": "ICC_A", "insured_value_pct": 110} if spec["mode"] in ("sea", "air", "rail") else {},
        price={}, liability_regime=None, forwarder_role="principal", quote_id=None, option_id=None, source="seed", actor=OPS, demo=True,
        reference=(keep or {}).get("reference"))
    if keep:
        s["id"] = keep["id"]  # upgrade in place: other slices (customs, invoices, messages) reference this id
    s["milestones"][1:] = ms
    booked_at = parse_iso(legs[0]["etd"]) - timedelta(days=rnd.uniform(3, 9))
    if booked_at > t0:
        booked_at = t0 - timedelta(hours=rnd.uniform(2, 30))
    s["milestones"][0].update(at=iso(booked_at), planned=iso(booked_at), by="Customer portal")
    s["created_at"] = iso(booked_at)
    s["price"] = await _price_estimate(s)
    s["seed_key"] = spec["key"]
    events = [{"code": "booked", "label": "Booking confirmed", "at": iso(booked_at), "source": "manual", "location": s["origin"]}]

    # ---- progress the life cycle up to now
    def achieve(code, at, source, by, leg_seq=None, note=None, location=None):
        m = svc._achieve(s["milestones"], code, at=at, source=source, by=by, location=location, leg_seq=leg_seq, note=note)
        svc._apply_leg_effects(s, code, at, m.get("leg_seq"))
        events.append({"code": code, "label": m["label"], "at": at, "source": source, "location": m.get("location"), "leg_seq": m.get("leg_seq"),
                       "note": note, "by": by})
        st = svc.STATUS_FOR_MILESTONE.get(code)
        if st:
            s["status"] = st
        s["milestone"] = code

    stop_at = None
    if target == "exception" and p.get("at") == "hub":
        stop_at = ("departed", 2)  # rolled: the feeder did not depart
    for m in list(s["milestones"]):
        if m.get("at") or not m.get("planned") or parse_iso(m["planned"]) > t0:
            if m.get("at"):
                continue
            break
        if stop_at and m["code"] == stop_at[0] and m.get("leg_seq") == stop_at[1]:
            break
        if m["code"] == "departed" and target == "live_train":
            achieve(m["code"], m["planned"], "gps", f"Train {train['number']} (rata.digitraffic.fi)", m.get("leg_seq"))
        elif m["code"] in svc.SCHEDULE_ACHIEVABLE:
            achieve(m["code"], m["planned"], "schedule", "Schedule", m.get("leg_seq"))
        elif target in ("delivered", "out_for_delivery") and m["code"] in ("customs_released", "export_cleared"):
            achieve(m["code"], m["planned"], "manual", "Customs broker", m.get("leg_seq"), note="Declaration accepted and released")
        elif target == "delivered" and m["code"] == "out_for_delivery":
            achieve(m["code"], m["planned"], "partner" if any(lg.get("carrier_id") == partner["id"] for lg in legs) else "manual",
                    "Driver (demo)", m.get("leg_seq"))
        elif target == "delivered" and m["code"] == "delivered":
            achieve(m["code"], m["planned"], "manual", "Huolitsija", m.get("leg_seq"), note="POD received", location=s["destination"])
        elif target == "out_for_delivery" and m["code"] == "out_for_delivery":
            achieve(m["code"], m["planned"], "partner" if any(lg.get("carrier_id") == partner["id"] for lg in legs) else "manual",
                    "Driver (demo)", m.get("leg_seq"))
        else:
            break
    if target == "customs_hold":
        since = iso(t0 - timedelta(hours=rnd.uniform(4, 18)))
        s["holds"] = [{"type": p["hold"], "reason": p["reason"], "since": since, "by": "Customs broker", "prev_status": s["status"]}]
        achieve("customs_hold", since, "manual", "Customs broker", note=p["reason"])
    if target == "exception":
        when = iso(t0 - timedelta(hours=rnd.uniform(3, 20)))
        achieve("exception", when, "manual", "Huolitsija", note=p["note"])
        s["has_exception"] = True
        s["exception_note"] = p["note"]
    if target == "delivered":
        s["delivered_at"] = next((m["at"] for m in s["milestones"] if m["code"] == "delivered" and m.get("at")), s.get("delivered_at"))
    # slipped ETA for exceptions: later legs move, the promised ETA stays as eta_planned
    if p.get("delay_days"):
        d = timedelta(days=p["delay_days"])
        cur = next((lg for lg in s["legs"] if not lg.get("ata")), None)
        for lg in s["legs"]:
            if cur and lg["seq"] >= cur["seq"]:
                if not lg.get("atd"):
                    lg["etd"] = iso(parse_iso(lg["etd"]) + d)
                lg["eta"] = iso(parse_iso(lg["eta"]) + d)
        for m in s["milestones"]:
            if not m.get("at") and m.get("planned") and cur and (m.get("leg_seq") or 0) >= cur["seq"]:
                m["planned"] = iso(parse_iso(m["planned"]) + d)
        s["eta"] = s["legs"][-1]["eta"]
        events.append({"code": "eta_changed", "label": f"ETA moved by {p['delay_days']} days", "at": iso(t0 - timedelta(hours=1)),
                       "source": "manual", "by": "Huolitsija"})
    s["eta"] = s["legs"][-1]["eta"] if not p.get("delay_days") else s["eta"]
    if not p.get("delay_days"):
        s["eta_planned"] = s["eta"]
    if target == "live_vessel":
        s["eta_planned"] = s["eta"]
    s["updated_at"] = max((e["at"] for e in events), default=now_iso())
    s["_events"] = events
    return s


async def _price_estimate(s: dict) -> dict:
    """Demo price: indicative all-in total from distance, weight and mode (only for display of demo data)."""
    w = s.get("weight_kg") or 0
    km = s.get("distance_km") or 0
    if s["mode"] == "air":
        cents = int(max(450, w * 3.4 + 180) * 100)
    elif s["mode"] == "sea":
        teu = s.get("teu") or max(0.25, (s.get("volume_m3") or 1) / 33)
        cents = int((1350 * teu + km * 0.02 * teu + 420) * 100)
    elif s["mode"] == "rail":
        cents = int((km * 1.6 + w * 0.02 + 600) * 100)
    else:
        ldm = (s.get("cargo") or {}).get("ldm") or 2
        cents = int((km * 0.11 * ldm + 180) * 100)
    return {"total_cents": round(cents, -2), "currency": "EUR", "demo_estimate": True}


async def _insert(s: dict):
    events = s.pop("_events", [])
    await db.shipments.insert_one(dict(s))
    for e in events:
        await db.tracking_events.insert_one({"id": new_id(), "shipment_id": s["id"], "company_id": s["company_id"], "code": e["code"],
                                             "label": e["label"], "at": e["at"], "location": svc.place(e.get("location")) if e.get("location") else None,
                                             "source": e["source"], "leg_seq": e.get("leg_seq"), "note": e.get("note"), "by": e.get("by"),
                                             "raw": None, "demo": True})
    await positions.refresh_shipment(s["id"], publish_always=False)


async def _replace(s: dict):
    """Rebuild an existing demo shipment: same id/reference/company; fresh legs, milestones and demo tracking events.
    Fields other slices may have written (documents, invoices, open exceptions) live in their own collections."""
    old = await db.shipments.find_one({"id": s["id"]}, NO_ID) or {}
    events = s.pop("_events", [])
    for k in ("customs_status", "documents_status", "billing", "exceptions_open"):
        if k in old and k not in s:
            s[k] = old[k]
    await db.shipments.replace_one({"id": s["id"]}, s)
    await db.tracking_events.delete_many({"shipment_id": s["id"], "demo": True})
    for e in events:
        await db.tracking_events.insert_one({"id": new_id(), "shipment_id": s["id"], "company_id": s["company_id"], "code": e["code"],
                                             "label": e["label"], "at": e["at"], "location": svc.place(e.get("location")) if e.get("location") else None,
                                             "source": e["source"], "leg_seq": e.get("leg_seq"), "note": e.get("note"), "by": e.get("by"),
                                             "raw": None, "demo": True})
    await positions.refresh_shipment(s["id"], publish_always=False)


# --------------------------------------------------------------------------------------------- partner user

async def ensure_partner_user(partner: dict):
    """Same rule as the other demo logins (seed_core._ensure_user): the env password only on creation or with
    DEMO_RESET_PASSWORDS=true, so a password changed in the app survives restarts."""
    from services.seed_core import _ensure_user

    u = await _ensure_user(PARTNER_EMAIL, "DEMO_PARTNER_PASSWORD", name="Esimerkkikumppani", role="partner", org_type="partner",
                           company_id=None, carrier_id=partner["id"], carrier_name=partner["name"], locale="fi", demo=True)
    return (u or {}).get("id")


async def relink_carriers():
    """When V2 seeds carriers after us (or later), link demo legs and the partner user to the real carrier records."""
    try:
        if not await db.carriers.estimated_document_count():
            return
    except Exception:  # noqa: BLE001
        return
    partner = await partner_carrier()
    if partner["id"] != FALLBACK_PARTNER_CARRIER["id"]:
        await db.users.update_one({"email": PARTNER_EMAIL, "carrier_id": FALLBACK_PARTNER_CARRIER["id"]},
                                  {"$set": {"carrier_id": partner["id"], "carrier_name": partner["name"]}})
    async for s in db.shipments.find({"demo": True}, {"_id": 0, "id": 1, "legs": 1}):
        changed = False
        for lg in s["legs"]:
            if lg.get("carrier_id") == FALLBACK_PARTNER_CARRIER["id"] and partner["id"] != FALLBACK_PARTNER_CARRIER["id"]:
                lg["carrier_id"], lg["carrier"] = partner["id"], partner["name"]
                changed = True
            elif lg.get("carrier") and not lg.get("carrier_id"):
                cid, cname = await carrier_ref(lg["carrier"])
                if cid:
                    lg["carrier_id"], lg["carrier"] = cid, cname
                    changed = True
        if changed:
            await db.shipments.update_one({"id": s["id"]}, {"$set": {"legs": s["legs"]}})


# --------------------------------------------------------------------------------------------- startup

# Container types use V2's ISO size-type codes (services/pricing). Earlier demo data said 20DV/40DV/40HQ.
CONTAINER_ALIASES = {"20DV": "20GP", "40DV": "40GP", "40HQ": "40HC", "45HQ": "45HC"}


async def fix_container_codes() -> int:
    n = 0
    for old, new in CONTAINER_ALIASES.items():
        r = await db.shipments.update_many({"cargo.containers.type": old}, {"$set": {"cargo.containers.$[c].type": new}},
                                           array_filters=[{"c.type": old}])
        n += r.modified_count
    # stored liability copies follow services.shipments.LIABILITY (Montreal is 26 SDR/kg from 2024-12-28)
    from services.shipments import LIABILITY

    for regime, terms in LIABILITY.items():
        r = await db.shipments.update_many({"liability_regime": regime, "liability": {"$ne": terms}}, {"$set": {"liability": terms}})
        n += r.modified_count
    return n


@on_startup(order=30)
async def seed_shipments():
    if not config.SEED_DEMO:
        return
    partner = await partner_carrier()
    await ensure_partner_user(partner)
    if await db.system.find_one({"key": MARKER}):
        await relink_carriers()
        await fix_container_codes()
        return
    # Older demo data (v1: great-circle air legs, road legs over the sea) is rebuilt in place, keeping id and reference.
    existing = {d["seed_key"]: d async for d in db.shipments.find({"demo": True, "seed_key": {"$exists": True}},
                                                                 {"_id": 0, "id": 1, "reference": 1, "seed_key": 1})}
    rnd = random.Random(20261024)
    vessels = await find_vessels()
    train = await find_running_cargo_train()
    used: set = set()
    n = 0
    for spec in specs():
        keep = existing.get(spec["key"])
        try:
            s = await build_one(spec, vessels, used, train, rnd, partner, keep=keep)
        except Exception:  # noqa: BLE001 - one bad spec must not stop the rest
            log.exception("demo shipment %s failed", spec["key"])
            continue
        if s:
            if keep:
                await _replace(s)
            else:
                await _insert(s)
            n += 1
    await db.system.update_one({"key": MARKER}, {"$set": {"at": now_iso(), "count": n, "vessels": sum(len(v) for v in vessels.values()),
                                                          "train": (train or {}).get("number")}}, upsert=True)
    log.info("demo shipments %s: %s (live train %s)", "upgraded" if existing else "seeded", n, (train or {}).get("number"))
