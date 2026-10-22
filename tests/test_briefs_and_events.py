"""
The rule-based brief, itinerary wording, traffic phrasing, and events.
"""

from datetime import datetime, timedelta
from unittest import mock

import alerts
import app
import events


def brief(**ctx):
    return app._fallback_brief({"destination": "the Ferry Building", **ctx})


def test_clear_trip():
    b = brief(mode="drive")
    assert b["risk"] == "none" and b["recommendation"] == "No action needed."


def test_heavy_traffic_is_medium_risk_with_leave_by():
    b = brief(mode="drive", traffic_delta_min=12, leave_by_text="8:21 AM")
    assert b["risk"] == "medium" and b["recommendation"] == "Leave by 8:21 AM."


def test_rain_matters_more_on_foot():
    rain = {"alerts": ["Rain likely."]}
    assert brief(mode="drive", weather=rain)["risk"] == "low"
    assert brief(mode="walk", weather=rain)["risk"] == "medium"


def test_missed_connection_raises_risk():
    plan = {"steps": ["Take the N"], "live": ["You may miss the 30: the BART gets in at 8:24 AM"]}
    b = brief(mode="transit", transit=plan)
    assert b["risk"] == "medium" and "next option" in b["recommendation"]


def test_nearby_items_alone_are_no_risk():
    # 1:20 AM, nothing on the road, and the brief said "minor possible
    # friction" because a report sat within 2 km of the route. A note is not a risk.
    note = {"name": "Blocked sidewalk", "type": "report", "severity": "yellow", "reason": "1.4 km from route"}
    b = brief(mode="drive", disruptions=[note], traffic_delta_min=0)
    assert b["risk"] == "none" and b["headline"].startswith("Clear drive")
    assert "not expected to affect you: Blocked sidewalk" in b["why"]


def test_a_crash_on_the_route_is_still_high():
    crash = {"name": "Crash · I-80 E", "type": "incident", "severity": "red", "reason": "0.1 km from route"}
    assert brief(mode="drive", disruptions=[crash])["risk"] == "high"


def test_a_big_event_nearby_is_low_not_none():
    game = {"name": "Giants vs. Dodgers", "type": "event", "severity": "yellow", "reason": "Oracle Park"}
    assert brief(mode="drive", disruptions=[game])["risk"] == "low"


def test_long_term_work_zone_on_the_route_is_a_note():
    # 511 lists months-long work zones with no daily hours; being on one says
    # nothing about a crew being out. Crashes on the route still count.
    assert alerts.on_route_severity("construction", 0.1) == "yellow"
    assert alerts.on_route_severity("incident", 0.1) == "red"
    assert alerts.on_route_severity("incident", 1.5) == "yellow"
    assert alerts.on_route_severity("incident", 2.5) is None


def test_311_blocked_sidewalks_are_notes_with_honest_labels():
    import mock_data
    rows = [{"service_name": "Blocked Street and Sidewalk", "service_subtype": "blocked_sidewalk",
             "status_description": "Open", "requested_datetime": "2026-10-20T10:00:00",
             "address": "1 Main St", "lat": "37.79", "long": "-122.39"},
            {"service_name": "Blocked Street and Sidewalk", "service_subtype": "blocked_parking_space_or_strip",
             "status_description": "Open", "requested_datetime": "2026-10-20T10:00:00",
             "address": "2 Main St", "lat": "37.79", "long": "-122.39"}]
    response = mock.Mock(json=lambda: rows, raise_for_status=lambda: None)
    with mock.patch("mock_data.requests.get", return_value=response):
        found = mock_data.get_civic_events(37.79, -122.39)["events"]
    assert [e["name"] for e in found] == ["Blocked sidewalk", "Blocked parking space"]
    assert {e["severity"] for e in found} == {"yellow"}


def test_traffic_phrase():
    assert alerts.traffic_phrase({"traffic_delta_min": 7}) == "7 min slower than usual"
    assert alerts.traffic_phrase({"traffic_delta_min": 1}) == "about usual for that time"
    assert alerts.traffic_phrase({}) is None


def test_itinerary_wording():
    cable = {"kind": "transit", "line": "PH", "line_name": "Powell-Hyde Cable Car", "vehicle": "Cable car",
             "from_stop": "Powell St", "to_stop": "Hyde St", "stops": 1}
    # Not "Powell-Hyde Cable Car cable car", and "1 stop", not "1 stops".
    assert alerts.describe_segment(cable) == "Take the PH Powell-Hyde Cable Car from Powell St, 1 stop, get off at Hyde St"


def _game(start):
    return events._event("tm", "x", "Warriors vs. Lakers", "Chase Center", 18_000, 37.7680, -122.3877, start)


def test_events_only_during_their_busy_window():
    game = _game(datetime.fromisoformat("2030-01-15T19:30:00-08:00"))
    drawn = {"coords": [(-122.4056, 37.7785), (-122.3950, 37.7740), (-122.3893, 37.7786)], "eta_min": 15}
    with mock.patch("events.upcoming", return_value=[game]):
        evening = alerts.events_on_route({"next_at": "2030-01-15T18:30"}, drawn, "drive")
        noon = alerts.events_on_route({"next_at": "2030-01-15T12:00"}, drawn, "drive")
        transit = alerts.events_on_route({"next_at": "2030-01-15T18:30"}, drawn, "transit")
    assert evening and evening[0]["severity"] == "red" and "heavy traffic" in evening[0]["reason"]
    assert noon == []
    assert "crowded trains" in transit[0]["reason"]


def test_only_big_venues_count():
    assert events._venue("Chase Center") == ("Chase Center", 18_000)
    assert events._venue("The Chapel") is None


def test_ticketmaster_parsing_skips_small_cancelled_and_untimed():
    now = datetime.now().astimezone()
    stamp = (now + timedelta(days=1)).astimezone().strftime("%Y-%m-%dT%H:%M:%SZ")
    def ev(name, venue, status="onsale", timed=True):
        return {"id": name, "name": name, "url": "u",
                "dates": {"start": {"dateTime": stamp} if timed else {}, "status": {"code": status}},
                "classifications": [{"segment": {"name": "Music"}}],
                "_embedded": {"venues": [{"name": venue, "location": {"latitude": "37.768", "longitude": "-122.387"}}]}}
    page = {"_embedded": {"events": [ev("Big show", "Chase Center"), ev("Bar gig", "The Chapel"),
                                     ev("Called off", "Chase Center", "canceled"), ev("TBA", "Chase Center", timed=False)]},
            "page": {"totalPages": 1}}
    response = mock.Mock(status_code=200, json=lambda: page, raise_for_status=lambda: None)
    with mock.patch("events.requests.get", return_value=response):
        found = events._ticketmaster("key", now)
    assert [e["name"] for e in found] == ["Big show"]
