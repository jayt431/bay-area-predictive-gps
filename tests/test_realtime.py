"""
Live delays laid over a planned trip, with the 511 feeds replaced by fixtures.

The scenario is the real one that found two bugs: BART into Oakland, then a
short walk to the AC Transit 72, ten minutes later on the timetable.
"""

from __future__ import annotations

import copy
from datetime import datetime
from unittest import mock

import pytest

import realtime

PLAN = {"routes": [{"segments": [
    {"kind": "walk", "duration_s": 600},
    {"kind": "transit", "op": "BA", "line": "", "line_name": "Yellow line", "trip_id": "T1",
     "board_stop_id": "A", "alight_stop_id": "B", "route_id": "Yellow-N", "stop_ids": ["A", "B"],
     "depart": "2030-01-01T22:35:00-08:00", "arrive": "2030-01-01T22:54:00-08:00"},
    {"kind": "walk", "duration_s": 60},
    {"kind": "transit", "op": "AC", "line": "72", "line_name": "x", "trip_id": "T2",
     "board_stop_id": "C", "alight_stop_id": "D", "route_id": "72", "stop_ids": ["C", "D"],
     "depart": "2030-01-01T23:04:00-08:00", "arrive": "2030-01-01T23:09:00-08:00"},
    {"kind": "walk", "duration_s": 300},
]}]}
NOW = datetime.fromisoformat("2030-01-01T22:25:00-08:00").timestamp()


def annotate(bart: dict | None, alerts=()):
    feeds = {"BA": (NOW, {"T1": bart} if bart else {}), "AC": (NOW, {})}
    with mock.patch("realtime.trip_updates", side_effect=lambda op: feeds[op]), \
         mock.patch("realtime.service_alerts", side_effect=lambda op: list(alerts) if op == "BA" else []), \
         mock.patch("realtime.time", return_value=NOW):
        return realtime.annotate(copy.deepcopy(PLAN))["routes"][0]


def late(seconds):
    return {"canceled": False, "delays": {"A": seconds, "B": seconds}}


def test_on_time():
    route = annotate(late(0))
    assert route["live"]["summary"] == "Running to schedule" and not route["live"]["warnings"]


@pytest.mark.parametrize("delay,missed,tight", [(60, False, False), (480, False, True), (600, True, False)])
def test_connections(delay, missed, tight):
    ac72 = annotate(late(delay))["segments"][3]["live"]
    assert bool(ac72.get("missed_connection")) is missed
    assert bool(ac72.get("tight_connection")) is tight


def test_summary_comes_from_the_most_delayed_ride():
    # Bug: it said "running to schedule" while BART was 11 min late, because
    # it judged by the last ride (the 72), which had no live data yet.
    route = annotate(late(660))
    assert "11 min late" in route["live"]["summary"]
    assert route["segments"][3]["live"]["status"] == "scheduled"


def test_cancellation():
    route = annotate({"canceled": True, "delays": {}})
    assert route["segments"][1]["live"]["status"] == "canceled"
    assert any("cancelled" in w for w in route["live"]["warnings"])


def test_alerts_attach_to_their_line_only():
    on_line = {"id": "1", "header": "Delays on Yellow", "description": "", "url": "",
               "periods": [], "routes": {"Yellow-N"}, "stops": set()}
    other = {**on_line, "id": "2", "header": "Red line work", "routes": {"Red-S"}}
    route = annotate(late(0), alerts=[on_line, other])
    assert [a["header"] for a in route["segments"][1]["live"]["alerts"]] == ["Delays on Yellow"]


def test_no_live_data_for_trips_far_ahead():
    with mock.patch("realtime.time", return_value=NOW - 24 * 3600):
        out = realtime.annotate(copy.deepcopy(PLAN))["routes"][0]
    assert "live" not in out


def test_budget_keeps_room_for_traffic():
    realtime._calls.clear()
    for _ in range(realtime.REALTIME_PER_HOUR):
        realtime.record("realtime")
    assert not realtime.allowed("realtime")      # live transit's share is spent…
    assert realtime.allowed("traffic")           # …but the traffic feed still has room
    realtime._calls.clear()
