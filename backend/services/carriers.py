"""Carrier network: locations used by rate cards, distances, DG acceptance, performance and
carrier API integrations (DCSA Commercial Schedules, Maersk point-to-point schedules).

Integration status is derived from the environment, never stored as a claim:

    CARRIER_<CODE>_API_KEY        key / consumer key issued by the carrier's developer portal
    CARRIER_<CODE>_BASE_URL       DCSA endpoint base URL (dcsa type only)
    CARRIER_<CODE>_SANDBOX=true   key is a sandbox key -> status "sandbox"
    CARRIER_<CODE>_ENABLED=true   go live -> status "live"
    CARRIER_<CODE>_PORTAL_ACCOUNT partner-portal account id (partner_portal type: ops book in the carrier portal)

<CODE> is the carrier's `code` (e.g. MAEU, HLCU, FINNLINES). Without these variables the status is
"not_configured" and quotes use rate cards only.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import date

import httpx

from core.db import db
from core.util import NO_ID, now, parse_iso

log = logging.getLogger("carriers")

# ---------------------------------------------------------------------------------------------
# Locations used by the V2 rate network. Coordinates are WGS84 (lat, lon) of the port / terminal /
# airport area. V1's `locations` collection (services.geo) is the primary source; this table keeps
# pricing working for every node a rate card references.
# ---------------------------------------------------------------------------------------------
NETWORK_LOCATIONS: dict[str, dict] = {
    # Asia origins
    "CNSHA": {"name": "Shanghai", "country": "CN", "lat": 31.36, "lon": 121.62, "kinds": ["port", "city"], "aliases": ["Shanghai port", "Yangshan", "上海"]},
    "CNNGB": {"name": "Ningbo", "country": "CN", "lat": 29.93, "lon": 121.85, "kinds": ["port", "city"], "aliases": ["Ningbo-Zhoushan", "Beilun"]},
    "CNYTN": {"name": "Yantian (Shenzhen)", "country": "CN", "lat": 22.57, "lon": 114.27, "kinds": ["port"], "aliases": ["Shenzhen", "Yantian"]},
    "CNPVG": {"name": "Shanghai Pudong Airport", "country": "CN", "lat": 31.14, "lon": 121.81, "kinds": ["airport"], "aliases": ["PVG", "Pudong"]},
    "CNCDU": {"name": "Chengdu", "country": "CN", "lat": 30.78, "lon": 104.20, "kinds": ["rail", "city"], "aliases": ["Chengdu rail terminal", "Chengdu International Railway Port"]},
    "HKHKG": {"name": "Hong Kong", "country": "HK", "lat": 22.31, "lon": 113.92, "kinds": ["port", "airport", "city"], "aliases": ["HKG"]},
    "KRPUS": {"name": "Busan", "country": "KR", "lat": 35.08, "lon": 128.83, "kinds": ["port", "city"], "aliases": ["Pusan"]},
    "SGSIN": {"name": "Singapore", "country": "SG", "lat": 1.26, "lon": 103.78, "kinds": ["port", "airport", "city"], "aliases": ["SIN"]},
    "VNSGN": {"name": "Ho Chi Minh City (Cat Lai)", "country": "VN", "lat": 10.76, "lon": 106.79, "kinds": ["port", "city"], "aliases": ["Saigon", "Cat Lai", "HCMC"]},
    # European hubs
    "DEHAM": {"name": "Hamburg", "country": "DE", "lat": 53.53, "lon": 9.93, "kinds": ["port", "city", "road"], "aliases": ["Hafen Hamburg"]},
    "DEBRV": {"name": "Bremerhaven", "country": "DE", "lat": 53.58, "lon": 8.53, "kinds": ["port"], "aliases": []},
    "NLRTM": {"name": "Rotterdam", "country": "NL", "lat": 51.95, "lon": 4.05, "kinds": ["port", "city", "road"], "aliases": ["Maasvlakte"]},
    "BEANR": {"name": "Antwerp", "country": "BE", "lat": 51.28, "lon": 4.33, "kinds": ["port", "city"], "aliases": ["Antwerpen"]},
    "SEGOT": {"name": "Gothenburg", "country": "SE", "lat": 57.69, "lon": 11.84, "kinds": ["port", "city", "rail"], "aliases": ["Göteborg"]},
    "PLGDN": {"name": "Gdańsk", "country": "PL", "lat": 54.40, "lon": 18.71, "kinds": ["port", "city"], "aliases": ["Gdansk", "DCT Gdańsk"]},
    "DETRV": {"name": "Travemünde (Lübeck)", "country": "DE", "lat": 53.96, "lon": 10.87, "kinds": ["port"], "aliases": ["Travemunde", "Lübeck", "Lubeck"]},
    "DERSK": {"name": "Rostock", "country": "DE", "lat": 54.15, "lon": 12.10, "kinds": ["port", "city"], "aliases": []},
    "EETLL": {"name": "Tallinn", "country": "EE", "lat": 59.44, "lon": 24.77, "kinds": ["port", "city", "road"], "aliases": ["Reval", "Muuga"]},
    "EEPLA": {"name": "Paldiski", "country": "EE", "lat": 59.35, "lon": 24.05, "kinds": ["port"], "aliases": []},
    "SESTO": {"name": "Stockholm", "country": "SE", "lat": 59.33, "lon": 18.07, "kinds": ["port", "city", "road"], "aliases": ["Värtahamnen"]},
    "SEKPS": {"name": "Kapellskär", "country": "SE", "lat": 59.72, "lon": 19.06, "kinds": ["port"], "aliases": ["Kapellskar"]},
    "SEMMA": {"name": "Malmö", "country": "SE", "lat": 55.61, "lon": 13.00, "kinds": ["port", "city"], "aliases": ["Malmo"]},
    "SEJKG": {"name": "Jönköping", "country": "SE", "lat": 57.78, "lon": 14.16, "kinds": ["city", "road"], "aliases": ["Jonkoping"]},
    "SEORB": {"name": "Örebro", "country": "SE", "lat": 59.27, "lon": 15.21, "kinds": ["city", "rail"], "aliases": ["Orebro"]},
    "SEHAA": {"name": "Haparanda", "country": "SE", "lat": 65.84, "lon": 24.14, "kinds": ["rail", "road"], "aliases": []},
    "DKCPH": {"name": "Copenhagen", "country": "DK", "lat": 55.68, "lon": 12.57, "kinds": ["port", "city"], "aliases": ["København"]},
    "DEDUI": {"name": "Duisburg", "country": "DE", "lat": 51.43, "lon": 6.76, "kinds": ["rail", "city"], "aliases": []},
    "DEBER": {"name": "Berlin", "country": "DE", "lat": 52.52, "lon": 13.40, "kinds": ["city"], "aliases": []},
    "DEFRA": {"name": "Frankfurt am Main", "country": "DE", "lat": 50.04, "lon": 8.56, "kinds": ["airport", "city"], "aliases": ["FRA", "Frankfurt"]},
    "DEMUC": {"name": "Munich", "country": "DE", "lat": 48.14, "lon": 11.58, "kinds": ["city"], "aliases": ["München"]},
    "DELEJ": {"name": "Leipzig", "country": "DE", "lat": 51.34, "lon": 12.37, "kinds": ["city", "airport"], "aliases": []},
    "PLWAW": {"name": "Warsaw", "country": "PL", "lat": 52.23, "lon": 21.01, "kinds": ["city"], "aliases": ["Warszawa"]},
    "PLPOZ": {"name": "Poznań", "country": "PL", "lat": 52.41, "lon": 16.93, "kinds": ["city"], "aliases": ["Poznan"]},
    "LTKUN": {"name": "Kaunas", "country": "LT", "lat": 54.90, "lon": 23.90, "kinds": ["city", "road"], "aliases": []},
    "LTVNO": {"name": "Vilnius", "country": "LT", "lat": 54.69, "lon": 25.28, "kinds": ["city"], "aliases": []},
    "LVRIX": {"name": "Riga", "country": "LV", "lat": 56.95, "lon": 24.11, "kinds": ["port", "city"], "aliases": []},
    "TRIST": {"name": "Istanbul", "country": "TR", "lat": 41.26, "lon": 28.74, "kinds": ["airport", "city"], "aliases": ["IST"]},
    "QADOH": {"name": "Doha", "country": "QA", "lat": 25.27, "lon": 51.61, "kinds": ["airport", "city"], "aliases": ["DOH"]},
    "KZHRG": {"name": "Khorgos Gateway", "country": "KZ", "lat": 44.21, "lon": 80.37, "kinds": ["rail"], "aliases": ["Khorgos", "Horgos"]},
    "AZBAK": {"name": "Baku (Alat)", "country": "AZ", "lat": 39.94, "lon": 49.41, "kinds": ["port", "rail"], "aliases": ["Alat"]},
    "GEPTI": {"name": "Poti", "country": "GE", "lat": 42.15, "lon": 41.67, "kinds": ["port"], "aliases": []},
    "ROCND": {"name": "Constanța", "country": "RO", "lat": 44.17, "lon": 28.65, "kinds": ["port"], "aliases": ["Constanta"]},
    # United States (exports)
    "USNYC": {"name": "New York / New Jersey", "country": "US", "lat": 40.67, "lon": -74.10, "kinds": ["port", "airport", "city"], "aliases": ["New York", "Newark", "JFK", "NYC"]},
    "USSAV": {"name": "Savannah", "country": "US", "lat": 32.08, "lon": -81.09, "kinds": ["port"], "aliases": []},
    "USCHI": {"name": "Chicago", "country": "US", "lat": 41.98, "lon": -87.90, "kinds": ["airport", "city", "rail"], "aliases": ["ORD", "O'Hare"]},
    "USHOU": {"name": "Houston", "country": "US", "lat": 29.73, "lon": -95.27, "kinds": ["port", "city"], "aliases": []},
    # Finland
    "FIHEL": {"name": "Helsinki", "country": "FI", "lat": 60.21, "lon": 25.19, "kinds": ["port", "airport", "city", "road"], "aliases": ["Helsingfors", "Vuosaari", "Helsinki-Vantaa", "HEL", "Vantaa"]},
    "FIKTK": {"name": "Kotka", "country": "FI", "lat": 60.44, "lon": 26.88, "kinds": ["port", "city", "rail"], "aliases": ["Mussalo", "HaminaKotka"]},
    "FIHMN": {"name": "Hamina", "country": "FI", "lat": 60.55, "lon": 27.17, "kinds": ["port", "city"], "aliases": ["Fredrikshamn"]},
    "FIRAU": {"name": "Rauma", "country": "FI", "lat": 61.13, "lon": 21.46, "kinds": ["port", "city"], "aliases": ["Raumo"]},
    "FITKU": {"name": "Turku", "country": "FI", "lat": 60.43, "lon": 22.22, "kinds": ["port", "city", "road"], "aliases": ["Åbo", "Abo"]},
    "FINLI": {"name": "Naantali", "country": "FI", "lat": 60.47, "lon": 22.02, "kinds": ["port"], "aliases": ["Nådendal"]},
    "FIHKO": {"name": "Hanko", "country": "FI", "lat": 59.82, "lon": 22.97, "kinds": ["port"], "aliases": ["Hangö"]},
    "FITMP": {"name": "Tampere", "country": "FI", "lat": 61.50, "lon": 23.76, "kinds": ["city", "rail", "road"], "aliases": ["Tammerfors"]},
    "FIOUL": {"name": "Oulu", "country": "FI", "lat": 65.01, "lon": 25.47, "kinds": ["port", "city", "rail", "road"], "aliases": ["Uleåborg"]},
    "FILHI": {"name": "Lahti", "country": "FI", "lat": 60.98, "lon": 25.66, "kinds": ["city", "road"], "aliases": ["Lahtis"]},
    "FIJYV": {"name": "Jyväskylä", "country": "FI", "lat": 62.24, "lon": 25.75, "kinds": ["city", "road"], "aliases": ["Jyvaskyla"]},
    "FIKUO": {"name": "Kuopio", "country": "FI", "lat": 62.89, "lon": 27.68, "kinds": ["city", "road"], "aliases": []},
    "FIVAA": {"name": "Vaasa", "country": "FI", "lat": 63.10, "lon": 21.62, "kinds": ["port", "city"], "aliases": ["Vasa"]},
    "FIPOR": {"name": "Pori", "country": "FI", "lat": 61.48, "lon": 21.80, "kinds": ["port", "city"], "aliases": ["Björneborg"]},
    "FIKVO": {"name": "Kouvola", "country": "FI", "lat": 60.87, "lon": 26.70, "kinds": ["city", "rail"], "aliases": ["Kouvola RRT"]},
    "FILPP": {"name": "Lappeenranta", "country": "FI", "lat": 61.06, "lon": 28.19, "kinds": ["city"], "aliases": ["Villmanstrand"]},
    "FIRVN": {"name": "Rovaniemi", "country": "FI", "lat": 66.50, "lon": 25.73, "kinds": ["city", "airport"], "aliases": []},
    "FITOR": {"name": "Tornio", "country": "FI", "lat": 65.85, "lon": 24.15, "kinds": ["city", "rail", "road"], "aliases": ["Torneå"]},
}

EU_COUNTRIES = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT",
                "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}
SANCTIONED_COUNTRIES = {"RU", "BY"}  # domain-spec RULE K-2: compliance review, default deny


def network_location(locode: str | None) -> dict | None:
    if not locode:
        return None
    rec = NETWORK_LOCATIONS.get(locode.upper())
    if not rec:
        return None
    return {"unlocode": locode.upper(), **{k: v for k, v in rec.items() if k != "aliases"}}


def location_label(loc: dict | None) -> str:
    if not loc:
        return "—"
    code = loc.get("unlocode")
    name = loc.get("name") or code or "—"
    return f"{name} ({code})" if code and code not in name else name


def haversine_km(a: dict, b: dict) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


# Circuity factors when no routed path is available (road network / rail network vs great circle).
CIRCUITY = {"road": 1.25, "rail": 1.35, "sea": 1.30, "air": 1.0}
_distance_cache: dict[tuple, float] = {}


def leg_distance_km(mode: str, a: dict, b: dict) -> float:
    """Transport distance of a leg. Order: V1 services.geo route (when present) -> searoute for sea ->
    great-circle x circuity. Air adds the GLEC 95 km landing/take-off correction."""
    if not a or not b or a.get("lat") is None or b.get("lat") is None:
        return 0.0
    key = (mode, round(a["lat"], 2), round(a["lon"], 2), round(b["lat"], 2), round(b["lon"], 2))
    if key in _distance_cache:
        return _distance_cache[key]
    km = None
    try:  # V1 geo module (lazy: it may not exist yet)
        from services import geo  # type: ignore

        path = geo.route_path(mode, a, b)
        if path and len(path) >= 2:
            km = float(geo.path_length_km(path))
    except Exception:  # noqa: BLE001 - module missing or route not available
        km = None
    if not km and mode == "sea":
        try:
            import searoute

            feat = searoute.searoute([a["lon"], a["lat"]], [b["lon"], b["lat"]], units="km")
            km = float(feat["properties"]["length"])
        except Exception:  # noqa: BLE001
            km = None
    if not km:
        km = haversine_km(a, b) * CIRCUITY.get(mode, 1.25)
    if mode == "air":
        km = haversine_km(a, b) + 95.0
    _distance_cache[key] = round(km, 1)
    return _distance_cache[key]


# GLEC-style default emission intensities, well-to-wheel, g CO2e per tonne-km. Indicative defaults used
# when the carrier has no own factor (carrier.co2_factor_g_tkm overrides the main leg).
DEFAULT_CO2_G_TKM = {
    "FCL": 12.0, "LCL": 12.0, "FEEDER": 20.0, "RORO": 55.0, "ROAD_FTL": 62.0, "ROAD_LTL": 95.0,
    "HAULAGE": 110.0, "AIR": 600.0, "AIR_SHORT": 1100.0, "RAIL_FCL": 25.0, "RAIL_WAGON": 18.0,
}


# ---------------------------------------------------------------------------------------------
# Dangerous goods acceptance (domain-spec RULE D-2)
# ---------------------------------------------------------------------------------------------
def dg_acceptance(carrier: dict, dg: dict | None) -> tuple[bool, str | None]:
    """Does the carrier accept this DG class / UN number? Returns (accepted, reason if not)."""
    if not dg:
        return True, None
    un = (dg.get("un_number") or "").upper().replace(" ", "")
    if un and not un.startswith("UN"):
        un = f"UN{un}"
    cls = str(dg.get("class") or "").strip().upper()
    accepted = [str(c).upper() for c in carrier.get("dg_classes_accepted") or []]
    if un and un in [u.upper() for u in carrier.get("dg_un_blocked") or []]:
        return False, f"{carrier['name']} does not carry {un}" + (f" ({carrier['dg_note']})" if carrier.get("dg_note") else "")
    if not accepted:
        return False, f"{carrier['name']} does not accept dangerous goods online; ask our ops team"
    if not cls:
        return True, None
    main = cls.split(".")[0]
    candidates = {cls, cls.rstrip("S").rstrip("ABCDEFGHJKLNS"), main}
    if candidates & set(accepted):
        return True, None
    return False, f"{carrier['name']} does not accept class {cls}"


# ---------------------------------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------------------------------
INTEGRATION_DOCS = {
    "maersk": "https://developer.maersk.com/",
    "dcsa": "https://dcsa.org/standards/",
    "dhl": "https://developer.dhl.com/",
    "partner_portal": None,
    "manual": None,
}


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _truthy(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes", "on")


def integration_env_names(carrier: dict) -> list[str]:
    code = (carrier.get("code") or "").upper().replace("-", "_").replace(" ", "_")
    itype = (carrier.get("integration") or {}).get("type", "manual")
    if itype == "dcsa":
        return [f"CARRIER_{code}_BASE_URL", f"CARRIER_{code}_API_KEY", f"CARRIER_{code}_ENABLED"]
    if itype in ("maersk", "dhl"):
        return [f"CARRIER_{code}_API_KEY", f"CARRIER_{code}_ENABLED"]
    if itype == "partner_portal":
        return [f"CARRIER_{code}_PORTAL_ACCOUNT", f"CARRIER_{code}_ENABLED"]
    return [f"CARRIER_{code}_ENABLED"]


def integration_status(carrier: dict) -> dict:
    """Live state of the carrier connection, computed from env (never trusted from the DB)."""
    integ = dict(carrier.get("integration") or {})
    itype = integ.get("type", "manual")
    names = integration_env_names(carrier)
    prefix = names[-1][: -len("_ENABLED")]  # CARRIER_<CODE>
    code = prefix[len("CARRIER_"):]
    required = [n for n in names if not n.endswith("_ENABLED")]
    have_all = all(_env(n) for n in required)
    if have_all and _truthy(f"{prefix}_ENABLED"):
        status = "live"
    elif have_all and required and _truthy(f"{prefix}_SANDBOX"):
        status = "sandbox"
    else:
        status = "not_configured"
    note = integ.get("note") or {
        "dcsa": "DCSA Commercial Schedules 1.0 / Booking 2.0 endpoint from the carrier's API portal.",
        "maersk": "Maersk Developer Portal consumer key (point-to-point schedules; Spot booking needs a commercial agreement).",
        "dhl": "DHL Global Forwarding API key from developer.dhl.com.",
        "partner_portal": "Bookings are placed by ops in the carrier's own booking portal and confirmed back here.",
        "manual": "Bookings by e-mail to the carrier's booking desk, confirmation recorded by ops.",
    }.get(itype)
    note_fi = integ.get("note_fi") or ({
        "dcsa": "DCSA Commercial Schedules 1.0 / Booking 2.0 -rajapinta kuljettajan API-portaalista.",
        "maersk": "Maersk Developer Portal -avain (aikataulut; spot-varaus vaatii kaupallisen sopimuksen).",
        "dhl": "DHL Global Forwarding -API-avain osoitteesta developer.dhl.com.",
        "partner_portal": "Ops tekee varaukset kuljettajan omassa varausportaalissa ja kirjaa vahvistuksen tänne.",
        "manual": "Varaukset sähköpostilla kuljettajan varauspalveluun; ops kirjaa vahvistuksen.",
    }.get(itype) if not integ.get("note") else note)
    return {"type": itype, "status": status, "env": names, "docs_url": integ.get("docs_url") or INTEGRATION_DOCS.get(itype),
            "note": note, "note_fi": note_fi, "code": code}


class DcsaSchedules:
    """DCSA Commercial Schedules 1.0 client (point-to-point routes). Used when a carrier is live."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def point_to_point(self, por: str, pod: str, departure_start: date) -> list[dict]:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(f"{self.base_url}/v1/point-to-point-routes",
                                 params={"placeOfReceipt": por, "placeOfDelivery": pod,
                                         "departureStartDate": departure_start.isoformat(), "limit": 5},
                                 headers={"API-Version": "1", "Authorization": f"Bearer {self.api_key}",
                                          "Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
        out = []
        for route in data if isinstance(data, list) else data.get("routes", []):
            por_ = route.get("placeOfReceipt") or {}
            pod_ = route.get("placeOfDelivery") or {}
            out.append({"etd": (por_.get("dateTime") or "")[:10], "eta": (pod_.get("dateTime") or "")[:10],
                        "transit_days": route.get("transitTime"), "raw": route})
        return out


class MaerskSchedules:
    """Maersk point-to-point schedules ("ocean products") with a Developer Portal consumer key."""

    URL = "https://api.maersk.com/products/ocean-products"

    def __init__(self, consumer_key: str):
        self.key = consumer_key

    async def point_to_point(self, origin: dict, destination: dict, start: date) -> list[dict]:
        params = {"vesselOperatorCarrierCode": "MAEU", "collectionOriginCountryCode": origin.get("country"),
                  "collectionOriginCityName": origin.get("name"), "deliveryDestinationCountryCode": destination.get("country"),
                  "deliveryDestinationCityName": destination.get("name"), "startDate": start.isoformat(), "dateRange": "P4W"}
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(self.URL, params=params, headers={"Consumer-Key": self.key, "Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
        out = []
        for prod in data.get("oceanProducts", []) if isinstance(data, dict) else []:
            for sched in prod.get("transportSchedules", []):
                dep = (sched.get("departureDateTime") or "")[:10]
                arr = (sched.get("arrivalDateTime") or "")[:10]
                out.append({"etd": dep, "eta": arr, "transit_days": sched.get("transitTime"), "raw": sched})
        return out


async def live_schedule(carrier: dict, origin: dict, destination: dict, start: date) -> dict | None:
    """First live sailing from the carrier API when its integration is live; None otherwise or on error."""
    st = integration_status(carrier)
    if st["status"] not in ("live", "sandbox"):
        return None
    prefix = f"CARRIER_{st['code']}"
    try:
        if st["type"] == "dcsa":
            rows = await DcsaSchedules(_env(f"{prefix}_BASE_URL"), _env(f"{prefix}_API_KEY")).point_to_point(
                origin.get("unlocode"), destination.get("unlocode"), start)
        elif st["type"] == "maersk":
            rows = await MaerskSchedules(_env(f"{prefix}_API_KEY")).point_to_point(origin, destination, start)
        else:
            return None
    except Exception as exc:  # noqa: BLE001 - live data is an enhancement; rate card schedule stays
        log.warning("live schedule %s failed: %s", carrier.get("code"), exc)
        return None
    rows = [r for r in rows if r.get("etd") and r.get("eta")]
    return min(rows, key=lambda r: r["etd"]) if rows else None


# ---------------------------------------------------------------------------------------------
# Performance (on-time %) from our own delivered legs; benchmark value until enough history exists
# ---------------------------------------------------------------------------------------------
MIN_SAMPLE = 10


async def carrier_performance(carrier: dict) -> dict:
    total = on_time = 0
    async for s in db.shipments.find({"legs.carrier_id": carrier["id"]}, {"_id": 0, "legs": 1}):
        for leg in s.get("legs") or []:
            if leg.get("carrier_id") != carrier["id"] or not leg.get("ata") or not leg.get("eta"):
                continue
            ata, eta = parse_iso(leg["ata"]), parse_iso(leg["eta"])
            if not ata or not eta:
                continue
            total += 1
            if (ata - eta).total_seconds() <= 86400:  # within one day of the promised ETA
                on_time += 1
    if total >= MIN_SAMPLE:
        return {"on_time_pct": round(100 * on_time / total, 1), "source": "own_shipments", "sample_size": total,
                "note": "Share of our delivered legs that arrived within one day of ETA.",
                "note_fi": "Osuus toimitetuista osuuksistamme, jotka saapuivat vuorokauden sisällä arvioidusta ajasta."}
    return {"on_time_pct": carrier.get("on_time_pct"), "source": "benchmark", "sample_size": total,
            "note": f"Indicative benchmark until {MIN_SAMPLE} legs with this carrier are delivered ({total} so far).",
            "note_fi": f"Suuntaa-antava vertailuluku, kunnes {MIN_SAMPLE} osuutta tällä kuljettajalla on toimitettu ({total} tähän mennessä)."}


async def get_carrier(carrier_id: str) -> dict | None:
    return await db.carriers.find_one({"id": carrier_id}, NO_ID)


def today() -> date:
    return now().date()
