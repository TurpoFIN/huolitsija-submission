"""Flight tracking stores a leg's flight as {callsign, date, icao24}. Live status mail for HUO-2026-000088 printed
"lento {'callsign': 'CCA827', ...}"; it must read "lento CCA827" (and PDFs must not crash or print the dict)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.util import flight_label
from services.comms_agent import _vehicle

FLIGHT = {"callsign": "CCA827", "date": "2026-10-25", "icao24": "781344"}


def test_flight_label():
    assert flight_label(FLIGHT) == "CCA827"
    assert flight_label("AY1331") == "AY1331"
    assert flight_label(None) == "" and flight_label({}) == ""


def test_status_mail_vehicle_prints_callsign_not_dict():
    s = {"mode": "air", "legs": [{"mode": "air", "flight": FLIGHT}]}
    assert _vehicle(s, "fi") == ", lento CCA827"
    assert _vehicle(s, "en") == " on flight CCA827"
    assert _vehicle({"mode": "air", "legs": [{"mode": "air", "flight": "AY1331"}]}, "fi") == ", lento AY1331"
