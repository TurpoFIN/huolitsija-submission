"""AIS identity disagreement is flagged, never silently resolved.

Doubles are captured from real responses on 2026-10-25: Digitraffic /api/ais/v1/vessels/230705000 (VIKING XPRS) and the
VesselAPI row live stored for MMSI 244890000 (reported as EN AVANT 25 while the Digitraffic port call 3408136 booked it
as PRIMA QUEEN, IMO 9194282).
"""
import asyncio

DIGITRAFFIC_VIKING_XPRS = {"callSign": "OJND", "destination": "FIHEL <> EEVAN", "draught": 65, "eta": 705802,
                           "imo": 9375654, "mmsi": 230705000, "name": "VIKING XPRS", "posType": 1, "shipType": 60,
                           "timestamp": 1792900616663}
VESSELAPI_244890000 = {"mmsi": "244890000", "at": "2026-10-25T03:55:40Z", "cog": 322.6, "heading": 294.0,
                       "lat": 54.63842, "lon": 11.37441, "name": "EN AVANT 25", "nav_status": 5, "sog": 0.0,
                       "source": "vesselapi"}


def test_vesselapi_name_mismatch_is_flagged():
    from services import positions
    booked = {"name": "PRIMA QUEEN", "imo": "9194282", "mmsi": "244890000"}
    r = asyncio.run(positions.vessel_identity(booked, VESSELAPI_244890000))
    assert r["state"] == "disagree" and r["fields"] == ["name"]
    assert r["reported"]["name"] == "EN AVANT 25" and r["booked"]["name"] == "PRIMA QUEEN" and r["provider"] == "vesselapi"


def test_digitraffic_metadata_agrees_and_imo_mismatch_flags(monkeypatch):
    from services import positions

    async def meta(mmsi):
        assert str(mmsi) == "230705000"
        return DIGITRAFFIC_VIKING_XPRS
    monkeypatch.setattr(positions, "vessel_metadata", meta)
    fix = {"mmsi": "230705000", "lat": 59.445515, "lon": 24.762912, "at": "2026-10-25T03:41:21Z", "source": "digitraffic"}
    ok = asyncio.run(positions.vessel_identity({"name": "Viking XPRS", "imo": "9375654", "mmsi": "230705000"}, fix))
    assert ok["state"] == "agree" and ok["fields"] == []
    bad = asyncio.run(positions.vessel_identity({"name": "VIKING XPRS", "imo": "9349461", "mmsi": "230705000"}, fix))
    assert bad["state"] == "disagree" and bad["fields"] == ["imo"] and bad["reported"]["imo"] == "9375654"


def test_unknown_when_provider_reports_nothing(monkeypatch):
    from services import positions

    async def meta(mmsi):
        return None
    monkeypatch.setattr(positions, "vessel_metadata", meta)
    r = asyncio.run(positions.vessel_identity({"name": "X", "imo": "1", "mmsi": "123456789"},
                                              {"source": "digitraffic", "lat": 0, "lon": 0, "at": "x"}))
    assert r["state"] == "unknown" and r["fields"] == []
