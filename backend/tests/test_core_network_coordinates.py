"""s4 D3 follow-up: one UN/LOCODE can name a harbour and an airport (FIHEL). Quote legs took whichever location record
came last, so a sea leg to Helsinki ended at Helsinki-Vantaa airport. The record now follows the leg's mode."""
from services import pricing

FIHEL = [{"code": "FIHEL", "unlocode": "FIHEL", "kinds": ["port", "road", "rail"], "rank": 100, "lat": 60.21502, "lon": 25.19392},
         {"code": "HEL", "unlocode": "FIHEL", "kinds": ["airport"], "rank": 60, "lat": 60.31836, "lon": 24.96334}]


def test_coordinates_follow_the_leg_mode(monkeypatch):
    for order in (FIHEL, list(reversed(FIHEL))):  # the database returns them in any order
        monkeypatch.setitem(pricing._LOC_OVERRIDE, "FIHEL", pricing._coordinates_by_kind("FIHEL", order))
        sea = pricing.endpoint_loc({"unlocode": "FIHEL"}, "sea")
        air = pricing.endpoint_loc({"unlocode": "FIHEL"}, "air")
        road = pricing.endpoint_loc({"unlocode": "FIHEL"}, "road")
        assert (sea["lat"], sea["lon"]) == (60.21502, 25.19392)
        assert (air["lat"], air["lon"]) == (60.31836, 24.96334)
        assert (road["lat"], road["lon"]) == (60.21502, 25.19392)  # primary record: code == UN/LOCODE
