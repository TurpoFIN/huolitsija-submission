"""Air, road and rail lanes must not cross Russia, Belarus or Ukraine (2026: EU carriers are banned from Russian and
Belarusian airspace, Ukrainian airspace is closed, EU sanctions and the closed eastern border rule out road and rail
transit through Russia/Belarus for this forwarder; see domain-spec sections 7 and 10).

Polygons: Natural Earth 1:110m (tests/fixtures/v1_closed_airspace_ne110m.json), plain ray casting, no shapely.
"""
import pytest

from test_v1_support import closed_airspace, country_at, ops, sample_path
from services import geo


def test_fixture_polygons_are_sane():
    # Moscow, Minsk and Kyiv are inside; Helsinki, Tallinn, Warsaw and Almaty are not.
    assert country_at(37.62, 55.75) == "RUS"
    assert country_at(27.56, 53.90) == "BLR"
    assert country_at(30.52, 50.45) == "UKR"
    for lon, lat in [(24.94, 60.17), (24.75, 59.44), (21.01, 52.23), (76.95, 43.24)]:
        assert country_at(lon, lat) is None
    assert set(closed_airspace()) == {"RUS", "BLR", "UKR"}


@pytest.mark.parametrize("a,b", [("PVG", "HEL"), ("HKG", "HEL"), ("ICN", "HEL"), ("NRT", "HEL"), ("PEK", "HEL"),
                                 ("SIN", "HEL"), ("BKK", "HEL"), ("DEL", "HEL"), ("DOH", "HEL"), ("DXB", "HEL"),
                                 ("HEL", "PVG"), ("PVG", "FRA"), ("HKG", "ARN"), ("DOH", "WAW"), ("JFK", "HEL"), ("ORD", "HEL")])
def test_air_routing_avoids_closed_airspace(a, b):
    info = geo.route_info("air", a, b)
    hits = [(p, country_at(*p)) for p in sample_path(info["path"]) if country_at(*p)]
    assert not hits, f"{a}->{b} crosses {hits[0][1]} at {hits[0][0]} ({len(hits)} samples)"
    gc = geo.haversine_km(info["path"][0], info["path"][-1])
    assert info["distance_km"] < gc * 1.7, f"{a}->{b}: {info['distance_km']} km is an implausible detour over {gc:.0f} km"


def test_map_snapshot_air_road_rail_paths_avoid_ru_by_ua():
    r = ops().get("/map/shipments", params={"scope": "ops"})
    assert r.status_code == 200, r.text
    ships = r.json()["shipments"]
    assert ships, "no shipments on the map"
    checked = 0
    for s in ships:
        for lg in s["legs"]:
            if lg["mode"] not in ("air", "road", "rail"):
                continue
            checked += 1
            for p in sample_path(lg["path"]):
                c = country_at(*p)
                assert c is None, f"{s['reference']} leg {lg['seq']} ({lg['mode']} {lg['from']['name']} -> {lg['to']['name']}) is inside {c} at {p}"
    assert checked >= 20
