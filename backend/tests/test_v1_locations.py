"""Locations API over the real UN/LOCODE + OurAirports + curated Finnish terminal dataset (GET /api/locations)."""
from test_v1_support import API
import httpx


def get(path, **params):
    return httpx.get(f"{API}{path}", params=params, timeout=30)


def test_search_finnish_terminals_first():
    r = get("/locations", q="vuosaari")
    assert r.status_code == 200
    items = r.json()["items"]
    assert items[0]["code"] == "FIHEL" and "port" in items[0]["kinds"]
    assert abs(items[0]["lat"] - 60.215) < 0.01 and abs(items[0]["lon"] - 25.194) < 0.01


def test_kind_filter_and_iata():
    r = get("/locations", q="Hamburg", kind="port")
    assert r.status_code == 200 and r.json()["items"][0]["code"] == "DEHAM"
    r = get("/locations", q="HEL", kind="airport")
    hel = r.json()["items"][0]
    assert hel["iata"] == "HEL" and hel["country"] == "FI"


def test_finnish_towns_without_unlocode_coordinates_are_present():
    # UN/LOCODE has no coordinates for Hämeenlinna or Seinäjoki; the build fills them from Fintraffic station data.
    for q, code in [("Hämeenlinna", "FIHMY"), ("Seinäjoki", "FISJY"), ("Kouvola", "FIKVO")]:
        items = get("/locations", q=q, country="FI").json()["items"]
        assert any(i["code"] == code for i in items), f"{q}: {[i['code'] for i in items]}"


def test_detail_and_not_found_sentence():
    r = get("/locations/FIKVO")
    assert r.status_code == 200 and r.json()["name"].startswith("Kouvola")
    r = get("/locations/ZZZZZ")
    assert r.status_code == 404
    assert r.json()["detail"].endswith(".")


def test_sanctioned_countries_are_not_bookable_locations():
    for q in ("Moscow", "Minsk", "St Petersburg"):
        items = get("/locations", q=q, limit=50).json()["items"]
        assert not [i for i in items if i["country"] in ("RU", "BY")], q


def test_limit_is_bounded():
    assert get("/locations", q="port", limit=0).status_code == 422
    assert get("/locations", q="port", limit=101).status_code == 422
    assert len(get("/locations", q="", limit=100).json()["items"]) == 100
