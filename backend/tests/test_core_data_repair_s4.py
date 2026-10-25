"""One-time s4 data corrections (services/data_repair_s4.py). Fixtures are the leg states the code before 5d1e59b wrote
(observed through the API on that commit: picked up -> received -> departed put the sea leg at 100 %)."""
import copy

from services.data_repair_s4 import rename_legacy_demo_names, unstamp_premature_legs

P, R, D, A = "2026-10-24T14:20:32Z", "2026-10-24T14:20:33Z", "2026-10-24T14:20:40Z", "2026-10-30T08:00:00Z"


def ms(*pairs):
    return [{"code": c, "at": at, "leg_seq": seq} for c, at, seq in pairs]


def leg(seq, mode="sea", role="main", atd=None, ata=None):
    return {"seq": seq, "mode": mode, "role": role, "atd": atd, "ata": ata,
            "status": "completed" if ata else "in_progress" if atd else "planned"}


def test_single_sea_leg_received_and_departed():
    s = {"status": "in_transit", "legs": [leg(1, atd=P, ata=R)],
         "milestones": ms(("picked_up", P, None), ("received", R, None), ("departed", D, 1))}
    assert unstamp_premature_legs(s)
    assert s["legs"][0] == leg(1, atd=D)  # sailed at the departure, not arrived


def test_main_and_feeder_legs():
    s = {"status": "in_transit", "legs": [leg(1, atd=P, ata=R), leg(2, role="feeder")],
         "milestones": ms(("picked_up", P, None), ("received", R, None), ("departed", D, 1))}
    assert unstamp_premature_legs(s)
    assert (s["legs"][0]["atd"], s["legs"][0]["ata"], s["legs"][1]["atd"]) == (D, None, None)


def test_departure_that_went_to_the_next_leg_is_moved_back():
    s = {"status": "in_transit", "legs": [leg(1, atd=P, ata=D), leg(2, mode="road", role="delivery", atd=D)],
         "milestones": ms(("picked_up", P, None), ("departed", D, None))}
    assert unstamp_premature_legs(s)
    assert [(lg["atd"], lg["ata"], lg["status"]) for lg in s["legs"]] == [(D, None, "in_progress"), (None, None, "planned")]


def test_picked_up_but_not_departed_is_back_at_origin():
    s = {"status": "picked_up", "legs": [leg(1, mode="air", atd=P)], "milestones": ms(("picked_up", P, None))}
    assert unstamp_premature_legs(s) and s["legs"][0] == leg(1, mode="air")


def test_real_data_is_left_alone():
    cases = [
        # pickup truck leg ends at the terminal: received completes it
        {"status": "picked_up", "legs": [leg(1, mode="road", role="pickup", atd=P, ata=R), leg(2)],
         "milestones": ms(("picked_up", P, 1), ("received", R, 1))},
        # road main leg: collection is the departure
        {"status": "in_transit", "legs": [leg(1, mode="road", atd=P)], "milestones": ms(("picked_up", P, 1), ("departed", P, 1))},
        # sea leg departed and arrived as recorded
        {"status": "at_hub", "legs": [leg(1, atd=D, ata=A)], "milestones": ms(("departed", D, 1), ("arrived", A, 1))},
        # delivered shipments keep their legs
        {"status": "delivered", "legs": [leg(1, atd=P, ata=R)], "milestones": ms(("picked_up", P, None), ("received", R, None))},
    ]
    for s in cases:
        before = copy.deepcopy(s)
        assert not unstamp_premature_legs(s) and s == before, before


def test_demo_names_are_rewritten_everywhere_in_a_record():
    row = {"id": "c1", "kyc": {"eori": {"by": {"id": "u1", "name": "Ops Demo"},
                                        "summary_fi": "EORI FI3991426-4 vahvistettu käsin (Ops Demo, 2026-10-24): testi",
                                        "summary": "EORI FI3991426-4 verified manually by Ops Demo on 2026-10-24: test"}},
           "history": [{"by": "Partner Demo"}, {"by": "Customer Demo"}], "name": "Ops Demolition Oy"}
    out = rename_legacy_demo_names(row)
    assert out["kyc"]["eori"]["by"]["name"] == "Esimerkkikäyttäjä"
    assert "(Esimerkkikäyttäjä, 2026-10-24)" in out["kyc"]["eori"]["summary_fi"] and "Ops Demo" not in out["kyc"]["eori"]["summary"]
    assert [h["by"] for h in out["history"]] == ["Esimerkkikumppani", "Asiakkaan edustaja"]
    assert out["name"] == "Ops Demolition Oy"  # whole names only
    assert rename_legacy_demo_names(out) == out


HARBOUR, AIRPORT = {"lat": 60.21502, "lon": 25.19392}, {"lat": 60.31836, "lon": 24.96334}
COORDS = {"FIHEL": {"primary": HARBOUR, "port": HARBOUR, "airport": AIRPORT}, "CNYTN": {"primary": {"lat": 22.57, "lon": 114.27}}}


def ep(code, pt):
    return {"unlocode": code, "name": code, **pt}


def test_sea_leg_and_its_truck_move_from_the_airport_to_the_harbour():
    from services.data_repair_s4 import airport_points_on_harbour_legs
    yantian = COORDS["CNYTN"]["primary"]
    legs = [{"mode": "sea", "from": ep("CNYTN", yantian), "to": ep("FIHEL", AIRPORT)},
            {"mode": "road", "from": ep("FIHEL", AIRPORT), "to": ep("FILHI", {"lat": 60.98, "lon": 25.67})}]
    assert airport_points_on_harbour_legs(legs, COORDS) == [(0, "to", HARBOUR), (1, "from", HARBOUR)]


def test_air_legs_and_correct_harbour_legs_are_left_alone():
    from services.data_repair_s4 import airport_points_on_harbour_legs
    air = [{"mode": "air", "from": ep("CNPVG", {"lat": 31.1, "lon": 121.8}), "to": ep("FIHEL", AIRPORT)},
           {"mode": "road", "from": ep("FIHEL", AIRPORT), "to": ep("FILHI", {"lat": 60.98, "lon": 25.67})}]
    sea = [{"mode": "sea", "from": ep("CNYTN", COORDS["CNYTN"]["primary"]), "to": ep("FIHEL", HARBOUR)}]
    assert airport_points_on_harbour_legs(air, COORDS) == []
    assert airport_points_on_harbour_legs(sea, COORDS) == []
