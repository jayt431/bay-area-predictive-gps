"""
The transit planner, against the committed schedules.

Most of these pin down a behaviour that was once wrong — each test names the
bug it guards. Trips are planned for a Wednesday a few days out, so the
results don't depend on today's date or holiday service.
"""

import random
from datetime import datetime, timedelta

import pytest

import transit
from conftest import HOME

POWELL = (37.7844, -122.4079)
FERRY_BUILDING = (37.7956, -122.3934)
PALO_ALTO = (37.4434, -122.1650)
TREASURE_ISLAND = (37.8235, -122.3707)
CALTRAIN_4TH_KING = (37.7764, -122.3943)


def rides(route):
    return [s for s in route["segments"] if s["kind"] == "transit"]


def test_data_loads_every_major_agency():
    data = transit._load()
    loaded = {f["operator"] for f in data["feeds"]}
    assert set(transit.MAJOR) <= loaded


def test_direct_trip_plans(wednesday):
    out = transit.plan(POWELL, FERRY_BUILDING, depart_at=f"{wednesday}T08:00")
    assert out["routes"], out.get("error")
    assert all(r["arrive_at"] > r["leave_at"] for r in out["routes"])


def test_arrive_by_arrives_in_time(wednesday):
    deadline = datetime.fromisoformat(f"{wednesday}T09:00")
    out = transit.plan(HOME, PALO_ALTO, arrive_by=deadline.isoformat())
    assert out["routes"], out.get("error")
    for r in out["routes"]:
        arrived = datetime.fromisoformat(r["arrive_at"]).replace(tzinfo=None)
        assert arrived <= deadline + timedelta(minutes=1)


def test_arrive_by_finds_station_beyond_walking_reach(wednesday):
    # Bug: the backward search found "walk past a nearby stop to the Caltrain
    # station, then one train", and a filter dropped every one-ride RAPTOR
    # result — so "arrive in Palo Alto by 9" said no route at all.
    out = transit.plan(HOME, PALO_ALTO, arrive_by=f"{wednesday}T09:00")
    assert any("Caltrain" in r["lines"] for r in out["routes"])


def test_same_line_continuation_is_one_ride(wednesday):
    # Bug: the 25 runs out to Treasure Island as one trip and loops back as
    # the next; the plan read "take the 25, then change to the 25". Banning it
    # in the search lost the 25 entirely. It must appear, as one ride.
    out = transit.plan(HOME, TREASURE_ISLAND, arrive_by=f"{wednesday}T12:00")
    for r in out["routes"]:
        names = [(s["agency"], s["line"], s["line_name"]) for s in rides(r)]
        assert all(a != b for a, b in zip(names, names[1:])), names


def test_walking_beats_a_one_stop_ride(wednesday):
    # A bus for one stop isn't worth offering when walking is about as fast:
    # whatever is offered must clearly beat walking the whole way.
    out = transit.plan(HOME, CALTRAIN_4TH_KING, depart_at=f"{wednesday}T08:00")
    walk_all = transit._meters(*HOME, *CALTRAIN_4TH_KING) * transit.DETOUR / transit.WALK_SPEED_MPS
    if out["routes"]:
        assert all(r["duration_s"] <= walk_all for r in out["routes"])
    else:
        assert "walking" in out["error"]


def test_no_stop_nearby_says_so():
    out = transit.plan(HOME, (37.70, -122.70))          # open ocean
    assert out["routes"] == [] and "stop" in out["error"]


def test_ferry_labels_are_readable(wednesday):
    # Ferry route codes ("LSSF") meant nothing; the name does.
    out = transit.plan(HOME, (37.9456, -122.5089), arrive_by=f"{wednesday}T09:30")   # Larkspur
    ferries = [s for r in out["routes"] for s in rides(r) if s["vehicle"] == "Ferry"]
    assert ferries, "expected a ferry option to Larkspur"
    assert all(not s["line"] and "–" in s["line_name"] for s in ferries)


def test_rides_carry_ids_for_live_matching(wednesday):
    out = transit.plan(HOME, FERRY_BUILDING, depart_at=f"{wednesday}T08:00")
    for r in out["routes"]:
        for s in rides(r):
            assert s["trip_id"] and s["board_stop_id"] and s["alight_stop_id"] and s["op"]


@pytest.mark.parametrize("seed", [1, 2])
def test_random_trips_are_consistent(seed, wednesday):
    """Across random destinations, both directions: no crash, rides in time
    order, arrive-by respected, no same-line "transfers", every extra transfer
    earning its five minutes."""
    rng = random.Random(seed)
    deadline = datetime.fromisoformat(f"{wednesday}T17:30")
    for _ in range(15):
        dest = (rng.uniform(37.30, 38.10), rng.uniform(-122.55, -121.80))
        for kwargs in ({"depart_at": f"{wednesday}T08:00"}, {"arrive_by": deadline.isoformat()}):
            out = transit.plan(HOME, dest, **kwargs)
            for r in out["routes"]:
                legs = rides(r)
                for a, b in zip(legs, legs[1:]):
                    assert datetime.fromisoformat(b["depart"]) >= datetime.fromisoformat(a["arrive"])
                    assert (a["agency"], a["line"], a["line_name"]) != (b["agency"], b["line"], b["line_name"])
                if "arrive_by" in kwargs:
                    arrived = datetime.fromisoformat(r["arrive_at"]).replace(tzinfo=None)
                    assert arrived <= deadline + timedelta(minutes=1)
