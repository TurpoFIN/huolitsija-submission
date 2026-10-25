"""Boundary and persisted-flow checks, real Mongo; recorded provider schemas for parsers.
Unit examples below are explicitly synthetic protocol cases, never live service evidence.
"""
import asyncio
import copy
import json
import os
import sys
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

os.environ["DB_NAME"] = "huo_s10_tests_" + uuid.uuid4().hex[:8]
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)
from core.db import db, client, ensure_indexes
from core.util import now, now_iso, iso, parse_iso
from services.visibility.feeds import ais_message, adsb_rows, store_vessel, latest_vessel
from services.visibility.detect import findings, scan_shipment
from services.visibility.eta import predict_leg, congestion_from_calls
from services import positions
from services.tracking import DCSATrackAndTrace
from routes.visibility import shipment, milestones

def run(coro):
    return LOOP.run_until_complete(coro)

@pytest.fixture(scope="module", autouse=True)
def database():
    run(ensure_indexes())
    yield
    run(client.drop_database(os.environ["DB_NAME"]))
    client.close()
    LOOP.close()


@pytest.fixture(autouse=True)
def no_real_vesselapi_key(monkeypatch):
    # Tests never spend the real free-plan quota; VesselAPI tests set a unit-only key.
    monkeypatch.delenv("VESSELAPI_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def fresh_area_feed_window():
    # The area poll persists its provider back-off window; each test starts with none.
    run(db.raw.visibility_feeds.delete_many({"provider": "adsb.lol.area"}))


def ais():
    return {"MessageType":"PositionReport", "MetaData":{"MMSI":230123456,"ShipName":"TEST", "time_utc":now_iso()},
            "Message":{"PositionReport":{"Valid":True,"Latitude":60,"Longitude":25,"Sog":12,"TrueHeading":511}}}


def test_ais_sentinels_and_invalid_positions():
    a=ais(); row=ais_message(a)
    assert row["heading"] is None and row["sog"] == 12
    a["Message"]["PositionReport"]["Longitude"]=181
    assert ais_message(a) is None
    a=ais();a["Message"]["PositionReport"]["Valid"]=False
    assert ais_message(a) is None


def test_ais_missing_time_is_not_current():
    a=ais();del a["MetaData"]["time_utc"]
    assert ais_message(a) is None


def test_delayed_ais_cannot_overwrite_latest():
    row=ais_message(ais());run(store_vessel(row))
    old={**row,"at":iso(now()-timedelta(minutes=5)),"lat":10}
    assert run(store_vessel(old)) is False
    assert run(latest_vessel(row["mmsi"]))["lat"]==60


def test_stale_ais_not_live():
    row={**ais_message(ais()),"mmsi":"230123457","at":iso(now()-timedelta(hours=1))}
    run(store_vessel(row));assert run(latest_vessel(row["mmsi"])) is None


def test_adsb_uses_position_age_not_message_age():
    p={"hex":"abcdef","flight":"FIN123 ","lat":60,"lon":25,"seen_pos":3,"seen":0,"alt_baro":"ground","gs":0}
    d={"now":now().timestamp()*1000,"ac":[p]}
    row=adsb_rows(d,"adsb.lol")[0]
    assert row["callsign"]=="FIN123" and row["on_ground"] is True and row["alt_ft"] is None
    p["seen_pos"]=200
    assert adsb_rows(d,"adsb.lol")==[]
    del p["seen_pos"]
    assert adsb_rows(d,"adsb.lol")==[]


def test_reused_flight_callsign_requires_date(monkeypatch):
    from services.visibility import feeds
    fetch=AsyncMock(return_value=[]);monkeypatch.setattr(feeds,"fetch_flights",fetch)
    assert run(feeds.flight_fix({"flight":{"callsign":"FIN123","date":"2020-01-01"}})) is None
    fetch.assert_not_called()


def test_elapsed_schedule_never_achieves_milestone(monkeypatch):
    s={"id":"clock-test","company_id":"co","status":"booked","legs":[{"seq":1,"mode":"road"}],
       "milestones":[{"code":"picked_up","planned":iso(now()-timedelta(days=2)),"at":None,"leg_seq":1}]}
    run(db.raw.shipments.insert_one(copy.deepcopy(s)))
    monkeypatch.setattr(positions,"_evidence",AsyncMock(return_value=None))
    assert run(positions._advance(s)) is False
    stored=run(db.raw.shipments.find_one({"id":s["id"]}))
    assert stored["milestones"][0]["at"] is None
    assert milestones(stored)[0]["overdue"] is True


def leg():
    return {"seq":1,"mode":"sea","from":{"unlocode":"DEHAM","lat":53.5,"lon":9.9},
            "to":{"unlocode":"FIHEL","lat":60.2,"lon":25.2}}


def event(classifier="ACT",loc="DEHAM"):
    return {"eventID":"test-event","eventType":"TRANSPORT","transportEventTypeCode":"DEPA",
            "eventDateTime":now_iso(),"eventClassifierCode":classifier,"transportCall":{"UNLocationCode":loc}}


def test_dcsa_missing_classifier_never_means_actual():
    e=event();del e["eventClassifierCode"]
    assert DCSATrackAndTrace.normalise(e,leg(),{"legs":[leg()]}) is None


def test_dcsa_different_leg_departure_is_not_this_leg():
    assert DCSATrackAndTrace.normalise(event(loc="CNSHA"),leg(),{"legs":[leg()]}) is None


@pytest.mark.parametrize("classification",["PLN","EST","ACT"])
def test_dcsa_preserves_classification(classification):
    assert DCSATrackAndTrace.normalise(event(classification),leg(),{"legs":[leg()]})["classifier"]==classification


def test_dcsa_gate_out_not_last_mile_dispatch():
    e=event(loc="FIHEL");e.update(eventType="EQUIPMENT",equipmentEventTypeCode="GTOT")
    assert DCSATrackAndTrace.normalise(e,leg(),{"legs":[leg()]}) is None


def test_stationary_eta_is_unavailable():
    assert run(predict_leg(leg(),{"at":now_iso(),"speed_kn":0,"lon":20,"lat":58}))["state"]=="unavailable"


def test_eta_speed_sensitivity_and_source(monkeypatch):
    from services.visibility import eta
    monkeypatch.setattr(eta,"port_congestion",AsyncMock(return_value={"state":"unavailable"}))
    p=run(predict_leg(leg(),{"at":now_iso(),"speed_kn":12,"lon":20,"lat":58,"source":"ais"}))
    assert p["state"]=="predicted" and p["earliest"]<p["eta"]<p["latest"]
    assert p["calibrated"] is False and p["inputs"]["delay_applied_h"] is None
    assert p["inputs"]["routing"]=="searoute" and p["inputs"]["distance_km"]>100


def test_empty_congestion_is_unknown_not_zero():
    p=congestion_from_calls([])
    assert p["state"]=="insufficient_data" and p["arrival_delay_h_median"] is None


def test_no_false_cutoff_from_silence():
    s={"legs":[{**leg(),"cutoffs":[{"at":iso(now()-timedelta(days=1)),"source":"carrier_api"}]}]}
    assert findings(s)==[]


def test_rollover_requires_carrier_source():
    lg={**leg(),"original_etd":iso(now()),"etd":iso(now()+timedelta(days=4))}
    assert findings({"legs":[lg]})==[]
    lg["schedule_source"]="carrier_api"
    assert findings({"legs":[lg]})[0][0]=="R-ROLLOVER"


def test_visibility_tenant_isolation():
    from fastapi import HTTPException
    s={"id":"private","reference":"PRIVATE","company_id":"company-a","milestones":[]}
    run(db.raw.shipments.insert_one(s))
    with pytest.raises(HTTPException) as err:
        run(shipment("private",{"org_type":"customer","company_id":"company-b"}))
    assert err.value.status_code==404
    assert run(shipment("private",{"org_type":"customer","company_id":"company-a"}))["reference"]=="PRIVATE"


def test_exception_has_rule_before_event_and_deduplicates(monkeypatch):
    from services import exceptions_agent
    opened=[]
    async def capture(name,payload):
        if name=="exception.opened":opened.append(payload["exception"])
    monkeypatch.setattr(exceptions_agent,"emit",capture)
    lg={**leg(),"original_etd":iso(now()),"etd":iso(now()+timedelta(days=4)),"schedule_source":"carrier_api"}
    s={"id":"roll-test","company_id":"co","reference":"ROL-TEST","status":"in_transit","legs":[lg]}
    run(db.raw.shipments.insert_one(copy.deepcopy(s)))
    first=run(scan_shipment(s));second=run(scan_shipment(s))
    assert first[0]["id"]==second[0]["id"] and len(opened)==1
    assert opened[0]["rule"]=="R-ROLLOVER" and opened[0]["evidence"]["leg_seq"]==1
    from services.agents.exception_agent import plan_for
    assert "rebook" in plan_for(first[0],s)["actions"]


def test_terminal49_created_at_is_not_event_time():
    from services.visibility.carrier_feeds import Terminal49
    ev={"id":"protocol-example","attributes":{"event":"container.transport.vessel_departed","location_locode":"DEHAM","created_at":now_iso(),"timestamp":None}}
    assert Terminal49.normalise(ev,leg()) is None
    ev["attributes"]["timestamp"]=now_iso()
    assert Terminal49.normalise(ev,leg())["classifier"]=="ACT"
    ev["attributes"]["event"]="container.transport.estimated.vessel_departed"
    assert Terminal49.normalise(ev,leg())["classifier"]=="EST"


@pytest.mark.parametrize("timeout", [False, True])
def test_terminal49_free_plan_creates_once_and_never_claims_tracking(monkeypatch, timeout):
    import httpx
    from services.visibility import carrier_feeds
    original = httpx.AsyncClient
    posts = []
    def respond(request):
        if request.method == "GET":
            return httpx.Response(401, json={"errors": [{"detail": "You do not have permissions for using the API, except for creating tracking requests."}]})
        posts.append(request)
        if timeout:
            raise httpx.ReadTimeout("synthetic ambiguous provider result", request=request)
        return httpx.Response(201, json={"data": {"id": "protocol-only", "attributes": {"status": "pending"}}})
    monkeypatch.setattr(carrier_feeds.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    s = {"id": "synthetic", "references": {"bl_no": "UNIT-ONLY-" + str(timeout)}}
    adapter = carrier_feeds.Terminal49()
    if timeout:
        with pytest.raises(httpx.ReadTimeout): run(adapter.fetch(leg(), s))
    else:
        assert run(adapter.fetch(leg(), s)) == []
    assert run(adapter.fetch(leg(), s)) == []
    assert len(posts) == 1
    saved = run(db.raw.terminal49_requests.find_one({"request_number": s["references"]["bl_no"]}))
    assert saved["state"] == ("reconciliation_required" if timeout else "submitted")


@pytest.mark.parametrize("flag", ["demo", "test", "test_tenant", "is_demo", "is_test"])
def test_terminal49_never_creates_for_demo_cargo(flag):
    from services.visibility.carrier_feeds import Terminal49
    assert run(Terminal49().fetch(leg(), {flag: True, "references": {"bl_no": "UNIT-ONLY"}})) == []


REAL_REF = {"customer_ref": "TEST real-container tracking", "real_tracking_reference": True,
            "tracking_source_url": "https://example.org/public-source"}


@pytest.mark.parametrize("refs", [
    {"customer_ref": "TEST x"},                                                     # no real-reference label
    {**REAL_REF, "tracking_source_url": "http://example.org/x"},                    # source not https
    {**REAL_REF, "customer_ref": "customer order 1"},                               # not labelled TEST
])
def test_terminal49_test_tenant_needs_labelled_real_reference(refs):
    from services.visibility.carrier_feeds import Terminal49
    s = {"test_tenant": True, "references": {**refs, "container_numbers": ["MSKU0000000"]}}
    assert run(Terminal49().fetch(leg(), s)) == []


def test_terminal49_labelled_test_real_reference_reaches_provider_once(monkeypatch):
    import httpx
    from services.visibility import carrier_feeds
    original = httpx.AsyncClient
    posts = []
    def respond(request):
        if request.method == "GET":
            return httpx.Response(401, json={"errors": [{"detail": "You do not have permissions for using the API, except for creating tracking requests."}]})
        posts.append(json.loads(request.content))
        return httpx.Response(201, json={"data": {"id": "t49-req", "attributes": {"status": "pending"}}})
    monkeypatch.setattr(carrier_feeds.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    number = "TESTREAL" + uuid.uuid4().hex[:6].upper()
    s = {"id": "t", "test_tenant": True, "references": {**REAL_REF, "container_numbers": [number]}}
    assert run(carrier_feeds.Terminal49().fetch(leg(), s)) == []
    assert run(carrier_feeds.Terminal49().fetch(leg(), s)) == []
    assert [p["data"]["attributes"]["request_number"] for p in posts] == [number]
    saved = run(db.raw.terminal49_requests.find_one({"request_number": number}))
    assert saved["state"] == "submitted" and saved["provider_read"] == "denied_paid_plan_required"
    assert run(carrier_feeds.Terminal49().fetch(leg(), {**s, "demo": True})) == []


def test_recovered_17track_documented_payload_preserves_estimates():
    import json
    from pathlib import Path
    from services.visibility.courier_feeds import normalise_17track
    fixture = json.loads((Path(__file__).parent / "fixtures/s10_carrier/17track_doc_gettrackinfo.json").read_text())
    rows = [ev for item in fixture["gettrackinfo"]["data"]["accepted"] for ev in normalise_17track(item)]
    assert rows and all(ev["at"] for ev in rows)
    assert any(ev["classifier"] == "EST" for ev in rows)
    assert all(ev["code"] not in ("departed", "arrived") for ev in rows if ev["classifier"] == "ACT")


def test_recovered_dcsa_reference_events_preserve_classifiers():
    import json
    from pathlib import Path
    fixture = json.loads((Path(__file__).parent / "fixtures/s10_carrier/dcsa_refimpl_events.json").read_text())
    events = fixture["events"]
    normalised = []
    for ev in events:
        tc = ev.get("transportCall") or {}
        loc = tc.get("UNLocationCode") or (tc.get("facility") or {}).get("UNLocationCode")
        lg = {**leg(), "from": {"unlocode": loc}, "to": {"unlocode": loc}}
        row = DCSATrackAndTrace.normalise(ev, lg, {"legs": [lg]})
        if row:
            assert row["classifier"] == ev["eventClassifierCode"]
            assert row["location"]["unlocode"] == loc
            if tc.get("vessel"):
                assert row["vessel"]["name"] == tc["vessel"].get("vesselName", tc["vessel"].get("name"))
            normalised.append(row)
    assert normalised


def test_dhl_out_for_delivery_and_estimate_are_separate():
    from services.tracking import DHLUnifiedTracking
    rows = DHLUnifiedTracking.normalise_response({"shipments": [{"id": "protocol-only", "estimatedTimeOfDelivery": now_iso(), "events": [
        {"statusCode": "transit", "description": "Out for delivery", "timestamp": now_iso()}]}]})
    assert rows[0]["code"] == "out_for_delivery" and rows[0]["classifier"] == "ACT"
    assert rows[1]["code"] == "arrived" and rows[1]["classifier"] == "EST"


def test_stationary_observation_refresh_reaches_stored_position_and_event(monkeypatch):
    old = {"lat": 60, "lon": 25, "source": "ais", "updated_at": iso(now()-timedelta(minutes=5)), "speed_kn": 0}
    fresh = {**old, "updated_at": now_iso()}
    s = {"id": "stationary-test", "company_id": "test-only", "status": "in_transit", "position": old, "progress": .5}
    run(db.raw.shipments.insert_one(copy.deepcopy(s)))
    monkeypatch.setattr(positions, "compute_position", AsyncMock(return_value={"position": fresh, "progress": .5, "current_leg": 1}))
    monkeypatch.setattr(positions, "_splice_lane", AsyncMock(return_value=False))
    events = []
    monkeypatch.setattr(positions, "publish", events.append)
    run(positions.refresh_shipment(s["id"], publish_always=False))
    saved = run(db.raw.shipments.find_one({"id": s["id"]}))
    assert saved["position"]["updated_at"] == fresh["updated_at"]
    assert events[0]["position"]["updated_at"] == fresh["updated_at"]
    assert not positions._position_changed(fresh, fresh)
    assert not positions._position_changed({**old, "source": "eta_interpolation"}, {**fresh, "source": "eta_interpolation"})


def test_digitraffic_source_time_coordinates_and_sentinels():
    from services.visibility.feeds import digitraffic_rows
    feature = {"geometry": {"coordinates": [25, 60]}, "properties": {
        "mmsi": 230123456, "timestampExternal": now().timestamp()*1000 - 1000,
        "sog": 102.3, "cog": 360, "heading": 511}}
    row = digitraffic_rows({"features": [feature]})[0]
    assert row["source"] == "digitraffic" and row["mmsi"] == "230123456"
    assert row["sog"] is None and row["cog"] is None and row["heading"] is None
    for seconds in (-120, 901):
        feature["properties"]["timestampExternal"] = (now().timestamp()-seconds)*1000
        assert digitraffic_rows({"features": [feature]}) == []
    feature["properties"]["timestampExternal"] = now().timestamp()*1000
    feature["geometry"]["coordinates"] = [181, 60]
    assert digitraffic_rows({"features": [feature]}) == []


def test_delayed_area_fetch_cannot_regress_callsign_observation():
    from services.visibility.feeds import store_flight
    row = {"icao24": "test123", "callsign": "UNIT123", "at": now_iso(), "lat": 60, "lon": 25}
    run(store_flight(row))
    assert run(store_flight({**row, "at": iso(now()-timedelta(seconds=30)), "lat": 61})) is False
    assert run(db.raw.flight_positions.find_one({"icao24": row["icao24"]}))["lat"] == 60


def test_public_job_populates_routes_without_creating_cargo(monkeypatch):
    import httpx
    from services.visibility import feeds
    from routes.visibility import vessels, flights
    original = httpx.AsyncClient
    stamp = now().timestamp()*1000 - 1000
    def respond(request):
        if request.url.host == "meri.digitraffic.fi":
            return httpx.Response(200, json={"features": [{"geometry": {"coordinates": [24, 59]}, "properties": {
                "mmsi": 230999999, "timestampExternal": stamp, "sog": 8}}]})
        assert request.url.path == "/v2/point/60.3/24.9/250"
        return httpx.Response(200, json={"now": stamp, "ac": [{"hex": "ab1234", "flight": "UNIT12", "lat": 59,
            "lon": 24, "seen_pos": 1, "gs": 250}]})
    monkeypatch.setattr(feeds.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    before = run(db.raw.shipments.count_documents({}))
    assert run(feeds.public_observations({})) == {"vessels": 1, "flights": 1}
    assert any(r["mmsi"] == "230999999" for r in run(vessels({}))["positions"])
    assert any(r["icao24"] == "ab1234" for r in run(flights({}))["positions"])
    assert run(db.raw.shipments.count_documents({})) == before


def test_public_feed_failure_is_not_healthy_empty(monkeypatch):
    import httpx
    from services.visibility import feeds
    original = httpx.AsyncClient
    monkeypatch.setattr(feeds.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs))
    assert run(feeds.public_observations({})) == {"vessels": 0, "flights": 0}
    for provider in ("digitraffic", "adsb.lol.area"):
        assert run(db.raw.visibility_feeds.find_one({"provider": provider}))["state"] == "unavailable"
        run(feeds.health(provider, state="ok", observations=1))
        assert "error" not in run(db.raw.visibility_feeds.find_one({"provider": provider}))


def test_area_http_failure_reaches_status_and_clears_on_recovery(monkeypatch):
    import httpx
    from services.visibility import feeds
    from routes.visibility import status
    original = httpx.AsyncClient
    def respond(request):
        if request.url.host == "meri.digitraffic.fi":
            return httpx.Response(200, json={"features": []})
        return httpx.Response(429, headers={"Retry-After": "60"}, text="private provider body")
    monkeypatch.setattr(feeds.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs))
    assert run(feeds.public_observations({}))["flights"] == 0
    row = next(r for r in run(status({}))["feeds"] if r["provider"] == "adsb.lol.area")
    assert row["state"] == "unavailable" and row["http_status"] == 429
    assert row["retry_after_seconds"] == 60 and row["error"] == "HTTPStatusError"
    assert "private provider body" not in str(row)
    run(feeds.health("adsb.lol.area", state="unavailable", error="ConnectTimeout"))
    row = run(db.raw.visibility_feeds.find_one({"provider": "adsb.lol.area"}))
    assert "http_status" not in row and "retry_after_seconds" not in row
    run(feeds.health("adsb.lol.area", state="ok", observations=1))
    assert "error" not in run(db.raw.visibility_feeds.find_one({"provider": "adsb.lol.area"}))


def test_flight_failure_diagnostics_do_not_serialize_url_or_body():
    import httpx
    from services.visibility.feeds import flight_failure_fields
    request = httpx.Request("GET", "https://example.invalid/?token=private")
    response = httpx.Response(403, request=request, text="private", headers={"Retry-After": "private"})
    exc = httpx.HTTPStatusError("private exception", request=request, response=response)
    assert flight_failure_fields(exc) == {"error": "HTTPStatusError", "http_status": 403}


@pytest.mark.parametrize("age", [-10, 901])
def test_old_or_future_ais_cannot_drive_position_or_milestone(monkeypatch, age):
    from services.visibility import feeds
    monkeypatch.setattr(feeds, "latest_vessel", AsyncMock(return_value=None))
    monkeypatch.setattr(positions, "ais_location", AsyncMock(return_value={
        "lat": 55, "lon": 20, "at": iso(now()-timedelta(seconds=age)), "sog": 12, "age_s": age}))
    lg = {**leg(), "vessel": {"mmsi": "230123456"}}
    assert run(positions.live_fix(lg)) is None
    assert run(positions._evidence(lg, {"code": "departed", "planned": now_iso()})) is None


def test_direct_ais_lookup_validates_identity_and_persists_source(monkeypatch):
    response = type("Response", (), {"raise_for_status": lambda self: None, "json": lambda self: {
        "features": [{"geometry": {"coordinates": [25, 60]}, "properties": {
            "mmsi": 230111111, "timestampExternal": now().timestamp()*1000-1000, "sog": 102.3}}]}})()
    monkeypatch.setattr(positions, "_http", lambda: type("Client", (), {"get": AsyncMock(return_value=response)})())
    positions._cache.clear()
    assert run(positions.ais_location("230222222")) is None
    row = run(positions.ais_location("230111111"))
    assert row["sog"] is None and row["source"] == "digitraffic"
    assert run(latest_vessel("230111111"))["at"] == row["at"]


def test_area_poll_honours_provider_rate_limit(monkeypatch):
    import httpx
    from services.visibility import feeds
    original = httpx.AsyncClient
    calls = []
    def respond(request):
        if request.url.host == "meri.digitraffic.fi":
            return httpx.Response(200, json={"features": []})
        calls.append(request.url.path)
        return httpx.Response(429, headers={"x-ratelimit-limit": "4, 4;w=1, 60;w=60, 500;w=86400",
                                            "x-ratelimit-reset": "3600"})
    monkeypatch.setattr(feeds.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs))
    run(db.raw.visibility_feeds.delete_many({"provider": "adsb.lol.area"}))
    run(feeds.public_observations({}))
    row = run(db.raw.visibility_feeds.find_one({"provider": "adsb.lol.area"}))
    assert row["http_status"] == 429 and row["failures"] == 1
    assert 3500 <= (parse_iso(row["next_attempt_at"]) - now()).total_seconds() <= 3600
    run(feeds.public_observations({}))
    assert calls == ["/v2/point/60.3/24.9/250"]  # no request inside the provider's window
    run(db.raw.visibility_feeds.update_one({"provider": "adsb.lol.area"},
                                           {"$set": {"next_attempt_at": iso(now() - timedelta(seconds=1))}}))
    run(feeds.public_observations({}))
    assert len(calls) == 2
    row = run(db.raw.visibility_feeds.find_one({"provider": "adsb.lol.area"}))
    assert row["failures"] == 2


def test_area_poll_success_spaces_requests_within_daily_limit(monkeypatch):
    import httpx
    from services.visibility import feeds
    original = httpx.AsyncClient
    stamp = now().timestamp() * 1000 - 1000
    def respond(request):
        if request.url.host == "meri.digitraffic.fi":
            return httpx.Response(200, json={"features": []})
        return httpx.Response(200, json={"now": stamp, "ac": []}, headers={"x-ratelimit-remaining": "3"})
    monkeypatch.setattr(feeds.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs))
    run(db.raw.visibility_feeds.delete_many({"provider": "adsb.lol.area"}))
    run(feeds.public_observations({}))
    row = run(db.raw.visibility_feeds.find_one({"provider": "adsb.lol.area"}))
    assert row["state"] == "ok" and row["failures"] == 0 and "http_status" not in row
    wait = (parse_iso(row["next_attempt_at"]) - now()).total_seconds()
    assert feeds.FLIGHT_AREA_INTERVAL - 5 <= wait <= feeds.FLIGHT_AREA_INTERVAL
    assert 86400 / feeds.FLIGHT_AREA_INTERVAL <= 400


def _vesselapi_payload(mmsi="636016431", minutes_old=30, glitch=False):
    return {"vesselPosition": {"mmsi": int(mmsi), "imo": 9227340, "vessel_name": "UNIT VESSEL", "latitude": 1.25, "longitude": 103.8,
                               "timestamp": iso(now() - timedelta(minutes=minutes_old)), "suspected_glitch": glitch,
                               "cog": 90.0, "sog": 14.0, "heading": 91, "nav_status": 0}}


def _vesselapi_transport(monkeypatch, responses, calls):
    import httpx
    from services.visibility import feeds
    original = httpx.AsyncClient
    def respond(request):
        calls.append((request.url.path, request.headers.get("authorization", "")[:7]))
        return responses.pop(0) if responses else httpx.Response(500)
    monkeypatch.setattr(feeds.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))


def test_vesselapi_row_drops_glitches_and_keeps_source_time():
    from services.visibility.feeds import vesselapi_row
    row = vesselapi_row(_vesselapi_payload())
    assert row["source"] == "vesselapi" and row["mmsi"] == "636016431" and row["lat"] == 1.25
    assert vesselapi_row(_vesselapi_payload(glitch=True)) is None
    assert vesselapi_row({"vesselPosition": {"mmsi": 636016431, "latitude": 1, "longitude": 2}}) is None


def test_vesselapi_worldwide_fix_is_cached_and_budgeted(monkeypatch):
    import httpx
    from services.visibility import feeds
    monkeypatch.setenv("VESSELAPI_API_KEY", "unit-only")
    monkeypatch.setenv("VESSELAPI_DAILY_CALLS", "2")
    run(db.raw.visibility_feeds.delete_many({"provider": "vesselapi"}))
    run(db.raw.vesselapi_lookups.delete_many({}))
    calls = []
    _vesselapi_transport(monkeypatch, [httpx.Response(200, json=_vesselapi_payload("636016431")),
                                       httpx.Response(200, json=_vesselapi_payload("636016432"))], calls)
    a = run(feeds.vesselapi_location("636016431"))
    assert a["source"] == "vesselapi" and 1700 <= a["age_s"] <= 1900
    assert calls == [("/v1/vessel/636016431/position", "Bearer ")]
    assert run(feeds.vesselapi_location("636016431"))["lat"] == 1.25   # within refresh window: no call
    assert len(calls) == 1
    run(feeds.vesselapi_location("636016432"))
    run(db.raw.vesselapi_lookups.delete_many({}))
    run(feeds.vesselapi_location("636016431"))                          # daily budget of 2 is spent
    assert len(calls) == 2
    feed = run(db.raw.visibility_feeds.find_one({"provider": "vesselapi"}))
    assert feed["state"] == "ok" and feed["calls_today"] == 2


def test_vesselapi_rate_limit_backs_off(monkeypatch):
    import httpx
    from services.visibility import feeds
    monkeypatch.setenv("VESSELAPI_API_KEY", "unit-only")
    run(db.raw.visibility_feeds.delete_many({"provider": "vesselapi"}))
    run(db.raw.vesselapi_lookups.delete_many({}))
    calls = []
    _vesselapi_transport(monkeypatch, [httpx.Response(429, headers={"Retry-After": "60"}, json={"error": "rate_limited"})], calls)
    assert run(feeds.vesselapi_location("636016433")) is None
    feed = run(db.raw.visibility_feeds.find_one({"provider": "vesselapi"}))
    assert feed["state"] == "unavailable" and feed["http_status"] == 429 and feed["next_attempt_at"] > now_iso()
    run(db.raw.vesselapi_lookups.delete_many({}))
    run(feeds.vesselapi_location("636016433"))
    assert len(calls) == 1


def test_vesselapi_not_configured_makes_no_request(monkeypatch):
    from services.visibility import feeds
    monkeypatch.delenv("VESSELAPI_API_KEY", raising=False)
    calls = []
    _vesselapi_transport(monkeypatch, [], calls)
    assert run(feeds.vesselapi_location("636016434")) is None and calls == []


def test_live_fix_falls_back_to_worldwide_ais(monkeypatch):
    import httpx
    from services.visibility import feeds
    monkeypatch.setenv("VESSELAPI_API_KEY", "unit-only")
    monkeypatch.setattr(positions, "ais_location", AsyncMock(return_value=None))
    run(db.raw.visibility_feeds.delete_many({"provider": "vesselapi"}))
    run(db.raw.vesselapi_lookups.delete_many({}))
    calls = []
    _vesselapi_transport(monkeypatch, [httpx.Response(200, json=_vesselapi_payload("636016435", minutes_old=45))], calls)
    fix = run(positions.live_fix({"mode": "sea", "vessel": {"mmsi": "636016435"}}))
    assert fix["source"] == "ais" and fix["provider"] == "vesselapi" and fix["lon"] == 103.8
    pred = run(predict_leg({"seq": 1, "mode": "sea", "to": {"lat": 51.9, "lon": 4.4}}, fix))
    assert pred["state"] == "unavailable" and pred["reason"] == "stale_position"  # 45 min old: shown, not predicted


def test_worldwide_fallback_never_revives_stale_regional_ais(monkeypatch):
    from services.visibility import feeds
    monkeypatch.setenv("VESSELAPI_API_KEY", "unit-only")
    run(db.raw.vesselapi_lookups.update_one({"mmsi": "230123457"}, {"$set": {"checked_at": now_iso()}}, upsert=True))
    run(feeds.store_vessel({"mmsi": "230123457", "lat": 60, "lon": 25, "at": iso(now() - timedelta(minutes=30)), "source": "digitraffic"}))
    assert run(feeds.vesselapi_location("230123457")) is None


def test_bulk_vessel_store_is_monotonic_and_tracks_only_newer():
    from services.visibility.feeds import store_vessels
    t0, t1 = iso(now() - timedelta(minutes=5)), iso(now() - timedelta(minutes=1))
    run(db.raw.vessel_positions.delete_many({"mmsi": {"$in": ["230900001", "230900002"]}}))
    run(db.raw.vessel_track.delete_many({"mmsi": {"$in": ["230900001", "230900002"]}}))
    assert run(store_vessels([{"mmsi": "230900001", "lat": 60, "lon": 25, "at": t1, "source": "digitraffic"},
                              {"mmsi": "230900002", "lat": 61, "lon": 25, "at": t0, "source": "digitraffic"}])) == 2
    # older frame for 001 never overwrites; newer frame for 002 does; a future frame is dropped
    assert run(store_vessels([{"mmsi": "230900001", "lat": 1, "lon": 1, "at": t0, "source": "digitraffic"},
                              {"mmsi": "230900002", "lat": 62, "lon": 25, "at": t1, "source": "digitraffic"},
                              {"mmsi": "230900003", "lat": 62, "lon": 25, "at": iso(now() + timedelta(minutes=5)), "source": "digitraffic"}])) == 1
    assert run(db.raw.vessel_positions.find_one({"mmsi": "230900001"}))["lat"] == 60
    assert run(db.raw.vessel_positions.find_one({"mmsi": "230900002"}))["lat"] == 62
    assert run(db.raw.vessel_track.count_documents({"mmsi": {"$in": ["230900001", "230900002"]}})) == 3
    assert run(db.raw.vessel_positions.find_one({"mmsi": "230900003"})) is None


def test_vesselapi_no_position_is_a_vessel_miss_not_an_outage(monkeypatch):
    # Body captured from the real API on 2026-10-25 (GET /v1/vessel/305574000/position, demo vessel Kilia): 404.
    import httpx
    import json as _json
    from services.visibility import feeds
    body = _json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "vesselapi_404_no_position.json")))
    assert body["error"]["code"] == "resource_missing"
    monkeypatch.setenv("VESSELAPI_API_KEY", "unit-only")
    monkeypatch.setenv("VESSELAPI_DAILY_CALLS", "4")
    run(db.raw.visibility_feeds.delete_many({"provider": "vesselapi"}))
    run(db.raw.vesselapi_lookups.delete_many({}))
    calls = []
    _vesselapi_transport(monkeypatch, [httpx.Response(404, json=body), httpx.Response(200, json=_vesselapi_payload("636016431"))], calls)
    assert run(feeds.vesselapi_location("305574000")) is None
    feed = run(db.raw.visibility_feeds.find_one({"provider": "vesselapi"}))
    assert feed["state"] == "ok" and feed["last_miss"]["mmsi"] == "305574000" and "http_status" not in feed
    # 7 h later (past the 6 h refresh) the missing vessel is not asked again, so the shipment vessel keeps the budget.
    run(db.raw.vesselapi_lookups.update_one({"mmsi": "305574000"}, {"$set": {"checked_at": iso(now() - timedelta(hours=7))}}))
    assert run(feeds.vesselapi_location("305574000")) is None and len(calls) == 1
    assert run(feeds.vesselapi_location("636016431"))["source"] == "vesselapi" and len(calls) == 2
    run(db.raw.vesselapi_lookups.update_one({"mmsi": "305574000"}, {"$set": {"checked_at": iso(now() - timedelta(hours=73))}}))
    run(feeds.vesselapi_location("305574000"))  # after 3 days it is asked again
    assert len(calls) == 3


def test_feed_milestones_carry_their_provenance(monkeypatch):
    # A real Digitraffic port-call record (captured 2026-10-25, Viking XPRS at FIHEL, call 3397658, with ATA).
    import json as _json
    from services.visibility import feeds
    pc = _json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "digitraffic_port_call_ata.json")))
    ata = next(a["ata"] for a in pc["portAreaDetails"] if a.get("ata"))
    monkeypatch.setattr(positions, "port_calls", AsyncMock(return_value=[pc]))
    to = {"name": "Helsinki", "unlocode": "FIHEL", "lat": 60.16, "lon": 24.95}
    lg = {"seq": 1, "mode": "sea", "from": {"name": "Tallinn", "unlocode": "EETLL", "lat": 59.44, "lon": 24.77}, "to": to,
          "eta": ata, "vessel": {"mmsi": str(pc["mmsi"]), "imo": str(pc["imoLloyds"])}}
    src, at, loc, note = run(positions._evidence(lg, {"code": "arrived", "planned": ata}))
    assert (src, loc) == ("port_call", to) and at == iso(parse_iso(ata))
    assert note.startswith(f"Digitraffic port call {pc['portCallId']} ATA")
    # AIS departure: the note names the feed, MMSI, fix time, position and distance from the port.
    monkeypatch.setattr(feeds, "latest_vessel", AsyncMock(return_value=None))
    fix = {"mmsi": str(pc["mmsi"]), "lat": 59.80, "lon": 24.90, "at": iso(now() - timedelta(seconds=60)), "sog": 18, "age_s": 60}
    monkeypatch.setattr(positions, "ais_location", AsyncMock(return_value=fix))
    src, at, loc, note = run(positions._evidence(lg, {"code": "departed", "planned": now_iso()}))
    assert src == "ais" and at == fix["at"] and loc is None
    assert note.startswith(f"AIS digitraffic MMSI {pc['mmsi']}: fix ") and "km from EETLL" in note


def test_port_call_eta_update_is_logged_as_the_feed(monkeypatch):
    import json as _json
    pc = _json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "digitraffic_port_call_ata.json")))
    new_eta = iso(now() + timedelta(hours=40))
    for a in pc["portAreaDetails"]:
        a.update(ata=None, atd=None, eta=new_eta)  # the same real record before arrival, agent ETA moved
    monkeypatch.setattr(positions, "port_calls", AsyncMock(return_value=[pc]))
    sid = "pc-eta-" + uuid.uuid4().hex[:6]
    lg = {"seq": 1, "mode": "sea", "from": {"name": "Tallinn", "unlocode": "EETLL", "lat": 59.44, "lon": 24.77},
          "to": {"name": "Helsinki", "unlocode": "FIHEL", "lat": 60.16, "lon": 24.95}, "etd": iso(now()),
          "eta": iso(now() + timedelta(hours=10)), "status": "planned", "distance_km": 80,
          "vessel": {"name": pc["vesselName"], "mmsi": str(pc["mmsi"]), "imo": str(pc["imoLloyds"])}}
    s = {"id": sid, "reference": "HUO-T-PCETA", "company_id": "co-pc", "status": "booked", "mode": "sea", "legs": [lg],
         "origin": lg["from"], "destination": lg["to"], "eta": lg["eta"], "eta_planned": lg["eta"], "milestones": [],
         "position": {"lat": 59.44, "lon": 24.77, "source": "eta_interpolation", "leg_seq": 1}, "created_at": now_iso()}
    run(db.raw.shipments.insert_one(copy.deepcopy(s)))
    run(positions._sync_port_call_eta(s))
    stored = run(db.raw.shipments.find_one({"id": sid}))
    assert stored["eta"] == new_eta and stored["legs"][0]["port_call"]["port_call_id"] == pc["portCallId"]
    ev = run(db.raw.tracking_events.find_one({"shipment_id": sid, "code": "eta_changed"}))
    assert ev["source"] == "port_call" and ev["by"] == "Digitraffic port call"
    assert ev["note"] == f"Digitraffic port call {pc['portCallId']}: agent-reported ETA"
