"""services/geo.py against the real routing sources: searoute's marine network, the public OSRM server, the curated
rail corridors (built from Fintraffic timetables) and great circles around closed airspace. In-process, no mocks."""
import pytest

import test_v1_support  # noqa: F401  (sets DB_NAME and sys.path before services import)
from services import geo


def max_segment_km(path):
    return max(geo.haversine_km(a, b) for a, b in zip(path, path[1:]))


def test_asia_europe_sea_goes_round_the_cape_not_panama():
    r = geo.route_info("sea", "CNSHA", "NLRTM")
    assert r["source"] == "searoute"
    lons = [p[0] for p in r["path"]]
    lats = [p[1] for p in r["path"]]
    assert min(lats) < -30, "expected the Cape of Good Hope (Red Sea avoided)"
    assert max(lons) < 125 and min(lons) > -20, "an Asia–Europe liner service never crosses the Pacific"
    assert 22000 < r["distance_km"] < 28000
    assert max_segment_km(r["path"]) <= geo.MAX_SEGMENT_KM + 0.5


def test_baltic_feeder_and_caspian_ferry():
    r = geo.route_info("sea", "DEHAM", "FIKTK")
    assert r["source"] == "searoute" and 1300 < r["distance_km"] < 1700 and "Kiel" in r["note"]  # feeders use the Kiel Canal
    assert any(abs(p[0] - 9.67) < 0.1 and abs(p[1] - 54.30) < 0.1 for p in r["path"])  # through Rendsburg
    assert "Kiel" not in (geo.route_info("sea", "DEHAM", "SEGOT")["note"] or "")  # Gothenburg is reached round Skagen
    c = geo.route_info("sea", "KZAAU", "AZALA")
    assert c["source"] == "searoute" and 350 < c["distance_km"] < 700


def test_road_uses_osrm_with_real_distance():
    r = geo.route_info("road", "FIHEL", "FITMP")
    assert r["source"] == "osrm", r["note"]
    assert 160 < r["distance_km"] < 230
    assert len(r["path"]) <= geo.MAX_PATH_POINTS + 60  # simplified to <= 400 vertices, then densified


def test_osrm_ferry_crossings_are_flagged():
    # Tallinn -> Helsinki "by road" is the ferry: the planner must book it as a sea leg.
    r = geo.route_info("road", "EETLL", "FIHEL")
    assert r["source"] == "osrm"
    assert r["note"] and "ferry" in r["note"]


def test_finnish_rail_follows_the_corridor():
    r = geo.route_info("rail", "FIKVO", "FIOUL")
    assert r["source"] == "rail_corridor" and r["corridors"]
    assert 550 < r["distance_km"] < 800


def test_middle_corridor_is_split_at_the_caspian():
    segs = geo.rail_segments("CNXIA", "DEDUI")
    assert [s["mode"] for s in segs] == ["rail", "sea", "rail"]
    assert segs[1]["from"] == "KZAAU" and segs[1]["to"] == "AZALA"


def test_air_long_haul_is_rerouted_and_densified():
    r = geo.route_info("air", "PVG", "HEL")
    assert r["note"] and "airspace" in r["note"]
    gc = geo.haversine_km(r["path"][0], r["path"][-1])
    assert gc * 1.2 < r["distance_km"] < gc * 1.6
    assert max_segment_km(r["path"]) <= geo.MAX_SEGMENT_KM + 0.5
    direct = geo.route_info("air", "JFK", "HEL")
    assert direct["note"] is None


@pytest.mark.parametrize("f", [0.0, 0.25, 0.5, 0.9, 1.0])
def test_interpolate_and_project_are_inverse(f):
    path = geo.route_info("sea", "PLGDN", "FIKTK")["path"]
    p = geo.interpolate(path, f)
    frac, off = geo.project_fraction(path, p["lon"], p["lat"])
    assert off < 0.5 and abs(frac - f) < 0.01


def test_geocoder_street_address():
    g = geo.geocode({"street": "Mannerheimintie 1", "postal_code": "00100", "city": "Helsinki", "country": "FI"})
    if g is None:
        pytest.skip("Both Nominatim and Photon refused the request (rate limit) - nothing cached, retried next run")
    assert abs(g["lat"] - 60.168) < 0.02 and abs(g["lon"] - 24.94) < 0.03
    assert g["source"] in ("nominatim", "photon")
