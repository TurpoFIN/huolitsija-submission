"""Map data API exactly per shared/map-data-contract.md, scope isolation, the SSE stream, and live positions checked
against the real feeds they come from (Digitraffic AIS, Fintraffic train GPS)."""
import json
import re
import time
from datetime import datetime, timezone

import httpx
import pytest

from test_v1_support import API, customer, customer_company_id, db, ops, partner
from services.geo import haversine_km
from services import geo

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?Z$")
UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
LOCODE_RE = re.compile(r"^[A-Z]{2}[A-Z2-9]{3}$")
STATUSES = {"booked", "picked_up", "in_transit", "at_hub", "customs_hold", "out_for_delivery", "delivered", "exception"}
ITEM_KEYS = {"id", "reference", "customer_name", "mode", "status", "origin", "destination", "legs", "position", "progress", "eta",
             "weight_kg", "teu", "has_exception", "eta_prediction"}
LEG_KEYS = {"seq", "mode", "from", "to", "path", "carrier", "etd", "eta", "atd", "ata"}


def snapshot(client, scope, include_test=True):
    r = client.get("/map/shipments", params={"scope": scope}, headers={"X-Include-Test-Tenants": str(include_test).lower()})
    assert r.status_code == 200, r.text
    return r.json()


def test_snapshot_matches_the_contract_exactly():
    # Contract acceptance uses the default operational view, excluding synthetic test rows.
    body = snapshot(ops(), "ops", include_test=False)
    assert set(body) == {"generated_at", "shipments"} and ISO_RE.match(body["generated_at"])
    ships = body["shipments"]
    assert 30 <= len(ships) <= 2000
    known_codes = set(geo._dataset()["by_unlocode"])
    for s in ships:
        L = s["reference"]
        assert set(s) == ITEM_KEYS, L
        assert UUID4_RE.match(s["id"]) and re.match(r"^HUO-\d{4}-\d{6}$", s["reference"])
        assert s["status"] in STATUSES and s["mode"] in {"sea", "road", "air", "rail", "multimodal"}
        stored = db.shipments.find_one({"id": s["id"]})
        for key in ("origin", "destination"):
            p = s[key]
            assert set(p) == {"name", "unlocode", "lat", "lon"}, (L, p)
            assert p["unlocode"] is None or (LOCODE_RE.match(p["unlocode"]) and p["unlocode"] in known_codes), (L, p)
            assert (p["lat"], p["lon"]) == (stored[key]["lat"], stored[key]["lon"]), (L, p)
        assert ISO_RE.match(s["eta"]) and isinstance(s["has_exception"], bool)
        assert 0 <= s["progress"] <= 1 and (s["progress"] < 1 or s["status"] == "delivered")
        assert set(s["position"]) == {"lat", "lon", "updated_at", "source"}
        assert s["position"]["source"] in {"carrier_api", "ais", "adsb", "gps", "eta_interpolation"} and ISO_RE.match(s["position"]["updated_at"])
        modes = {lg["mode"] for lg in s["legs"]}
        assert s["mode"] == ("multimodal" if len(modes) > 1 else next(iter(modes))), L
        prev = None
        for i, lg in enumerate(s["legs"], start=1):
            assert set(lg) == LEG_KEYS and lg["seq"] == i, L
            assert set(lg["from"]) == {"name", "lat", "lon"} and set(lg["to"]) == {"name", "lat", "lon"}
            assert isinstance(lg["carrier"], str) and len(lg["carrier"]) > 1
            assert ISO_RE.match(lg["etd"]) and ISO_RE.match(lg["eta"]) and lg["etd"] < lg["eta"]
            assert lg["atd"] is None or ISO_RE.match(lg["atd"])
            assert lg["ata"] is None or (lg["atd"] and ISO_RE.match(lg["ata"]))
            assert len(lg["path"]) >= 2 and all(-180 <= x <= 180 and -90 <= y <= 90 for x, y in lg["path"])
            assert max(haversine_km(a, b) for a, b in zip(lg["path"], lg["path"][1:])) <= 40.5, (L, i)
            if prev:
                assert haversine_km([prev["to"]["lon"], prev["to"]["lat"]], [lg["from"]["lon"], lg["from"]["lat"]]) <= 1, (L, i)
            prev = lg
        if s["status"] == "exception":
            assert s["has_exception"]
        assert s["has_exception"] == bool(stored.get("has_exception") or s["status"] == "exception")
        if s["mode"] in ("air", "road"):
            assert s["teu"] is None
    assert len({s["id"] for s in ships}) == len(ships)


def test_scope_isolation():
    cid = customer_company_id()
    name = db.companies.find_one({"id": cid})["name"]
    mine = snapshot(customer(), "customer")["shipments"]
    assert mine and {s["customer_name"] for s in mine} == {name}
    assert customer().get("/map/shipments", params={"scope": "ops"}).status_code == 403
    portal = snapshot(customer(), "portal")["shipments"]  # the portal's name for the customer scope
    assert {s["id"] for s in portal} == {s["id"] for s in mine}
    assert partner().get("/map/shipments", params={"scope": "customer"}).status_code == 403
    assert httpx.get(f"{API}/map/shipments", params={"scope": "customer"}).status_code == 401
    ops_ids = {s["id"] for s in snapshot(ops(), "ops")["shipments"]}
    assert {s["id"] for s in mine} <= ops_ids and len(ops_ids) > len(mine)


def read_sse(client, scope, seconds, arrivals=None):
    events, comments, headers = [], [], None
    t0 = time.monotonic()
    with client.stream("GET", "/map/stream", params={"scope": scope}, timeout=seconds + 10) as r:
        headers = r.headers
        assert r.status_code == 200
        buf = ""
        for chunk in r.iter_text():
            buf += chunk
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                if arrivals is not None:
                    arrivals.append(time.monotonic() - t0)
                if block.startswith(":"):
                    comments.append((time.monotonic() - t0, block))
                ev = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line and not line.startswith(":"))
                if ev.get("event") == "position":
                    events.append(json.loads(ev["data"]))
            if time.monotonic() - t0 > seconds:
                break
    return events, comments, headers


def test_sse_emits_position_events_and_heartbeats():
    arrivals: list[float] = []
    events, comments, headers = read_sse(customer(), "customer", 25, arrivals)
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["x-accel-buffering"] == "no" and "no-cache" in headers["cache-control"]
    assert "content-encoding" not in headers
    assert events, "no position event"
    mine = {s["id"] for s in snapshot(customer(), "customer")["shipments"]}
    for e in events:
        assert set(e) == {"id", "position", "progress", "status"} and set(e["position"]) == {"lat", "lon", "updated_at", "source"}
        assert e["id"] in mine  # a customer's stream never carries another company's shipments
    # Heartbeats fill silence: the client never waits more than ~10 s for a byte (the ops requirement is <= 15 s).
    # Position events count as traffic, so the first heartbeat may come later than 10 s after connecting.
    gaps = [b - a for a, b in zip([0.0] + arrivals, arrivals)]
    assert max(gaps) <= 12, f"stream silent for {max(gaps):.1f} s"
    assert comments, "no heartbeat in 25 s"


def test_live_vessel_position_is_the_real_ais_fix():
    live = [s for s in snapshot(ops(), "ops", include_test=False)["shipments"] if s["position"]["source"] == "ais"]
    if not live:
        pytest.skip("No demo vessel inside Digitraffic AIS coverage right now (vessels outside the Baltic are interpolated)")
    for s in live:
        d = ops().get(f"/shipments/{s['id']}").json()
        leg = next(lg for lg in d["legs"] if lg.get("vessel", {}) and lg["vessel"].get("mmsi") and lg.get("atd") and not lg.get("ata"))
        r = httpx.get("https://meri.digitraffic.fi/api/ais/v1/locations", params={"mmsi": leg["vessel"]["mmsi"]},
                      headers={"Digitraffic-User": "Huolitsija/1.0", "Accept-Encoding": "gzip"}, timeout=30).json()
        f = r["features"][0]
        km = haversine_km(f["geometry"]["coordinates"], [s["position"]["lon"], s["position"]["lat"]])
        ais_at = datetime.fromtimestamp(f["properties"]["timestampExternal"] / 1000, tz=timezone.utc)
        assert km < 15, f"{s['reference']} {leg['vessel']['name']}: our position is {km:.1f} km from AIS"
        assert (ais_at - datetime.fromisoformat(s["position"]["updated_at"].replace("Z", "+00:00"))).total_seconds() < 1800


def test_live_train_position_is_the_real_gps_fix():
    live = [s for s in snapshot(ops(), "ops", include_test=False)["shipments"] if s["position"]["source"] == "gps" and s["mode"] == "rail"]
    if not live:
        pytest.skip("The demo train finished its run; no rail GPS position on the map right now")
    for s in live:
        d = ops().get(f"/shipments/{s['id']}").json()
        t = next(lg["train"] for lg in d["legs"] if lg.get("train"))
        r = httpx.get(f"https://rata.digitraffic.fi/api/v1/train-locations/latest/{t['number']}",
                      headers={"Digitraffic-User": "Huolitsija/1.0", "Accept-Encoding": "gzip"}, timeout=30).json()
        if not r:
            pytest.skip(f"train {t['number']} no longer reports GPS")
        km = haversine_km(r[0]["location"]["coordinates"], [s["position"]["lon"], s["position"]["lat"]])
        assert km < 10, f"{s['reference']} train {t['number']}: {km:.1f} km from its GPS"


def test_stationary_ais_observation_updates_freshness():
    from services.positions import _position_changed

    prior = {"lat": 59.44552, "lon": 24.76289, "source": "ais", "updated_at": "2026-10-24T23:50:00Z"}
    fresh = {**prior, "updated_at": "2026-10-25T00:23:51Z"}
    assert _position_changed(prior, fresh), "New AIS observations must persist even while the vessel stays stationary"
    assert not _position_changed(fresh, dict(fresh)), "The identical observation should remain idempotent"
