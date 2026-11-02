"""
The clock (POST /api/tick): each alert at its time, once, and the calendar
re-read every few hours. Time is frozen by replacing scheduler.local_now;
routing and delivery are fakes.
"""

import json
import os
from datetime import datetime, timedelta
from unittest import mock

import pytest

import alerts
import push
import scheduler
import store

KEYS = {"VAPID_PUBLIC_KEY": "pub", "VAPID_PRIVATE_KEY": "priv"}
CONTEXT = {"destination": "x", "dest_lat": 37.79, "dest_lon": -122.39, "eta_min": 10,
           "distance_mi": 2, "disruptions": [], "parking": [], "mode": "drive"}
TOMORROW = (datetime.now() + timedelta(days=1)).date()


@pytest.fixture(autouse=True)
def clean(client):          # client first: it creates the tables
    for trip in store.list_trips():
        store.delete_trip(trip["id"])
    for key in (scheduler.ICS_URL_KEY, "calendar_auto_sync_at"):
        store.set_setting(key, None)
    push._save([{"endpoint": "https://fcm.googleapis.com/fcm/send/x",
                 "keys": {"p256dh": "p", "auth": "a"}}], "local")
    yield
    push._save([], "local")


def at(day, hour, minute=0):
    return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)


def tick(client, now, delivered=True):
    from pywebpush import WebPushException

    def fake_push(**kw):
        if not delivered:
            raise WebPushException("down", response=mock.Mock(status_code=500))
    with mock.patch.dict(os.environ, KEYS), mock.patch("scheduler.local_now", return_value=now), \
         mock.patch("alerts.build_context", return_value=CONTEXT), \
         mock.patch("pywebpush.webpush", side_effect=fake_push) as sent:
        body = client.post("/api/tick").get_json()
    return body, sent.call_count


def test_alert_times():
    # Before noon: the evening before. Noon on: that morning.
    assert alerts.alert_due_at(at(TOMORROW, 9)) == at(TOMORROW - timedelta(days=1), 20)
    assert alerts.alert_due_at(at(TOMORROW, 15)) == at(TOMORROW, 7)


def test_morning_trip_waits_for_the_evening_then_alerts_once(client):
    store.save_trip({"label": "Work", "destination": "x", "dest_lat": 37.79, "dest_lon": -122.39,
                     "arrive_at": at(TOMORROW, 9).isoformat(timespec="minutes")})
    today = TOMORROW - timedelta(days=1)

    body, pushes = tick(client, at(today, 15))
    assert body["waiting"] == 1 and pushes == 0
    body, pushes = tick(client, at(today, 20, 5))
    assert body["sent"] == 1 and pushes == 1
    body, pushes = tick(client, at(today, 20, 15))       # the next tick: already sent
    assert body["sent"] == 0 and pushes == 0


def test_failed_delivery_retries_hourly_not_every_tick(client):
    store.save_trip({"label": "Gym", "destination": "x", "dest_lat": 37.79, "dest_lon": -122.39,
                     "arrive_at": at(TOMORROW, 18).isoformat(timespec="minutes")})
    assert tick(client, at(TOMORROW, 7, 5), delivered=False)[0]["failed"] == 1
    assert tick(client, at(TOMORROW, 7, 15), delivered=False)[1] == 0
    assert tick(client, at(TOMORROW, 8, 6))[0]["sent"] == 1


def test_calendar_resyncs_every_few_hours(client):
    store.set_setting(scheduler.ICS_URL_KEY, "https://example.com/cal.ics")
    with mock.patch("scheduler.sync_calendar", return_value={"ok": True}) as sync:
        tick(client, at(TOMORROW, 8))
        tick(client, at(TOMORROW, 8, 10))
        tick(client, at(TOMORROW, 14, 5))
    assert sync.call_count == 2


def test_reply_names_no_trips(client):
    # The outside scheduler keeps a history of replies on its own site.
    store.save_trip({"label": "Therapist", "destination": "x", "dest_lat": 37.79, "dest_lon": -122.39,
                     "arrive_at": at(TOMORROW, 9).isoformat(timespec="minutes")})
    body, _ = tick(client, at(TOMORROW - timedelta(days=1), 21))
    assert "Therapist" not in str(body) and body["sent"] == 1


def test_tick_needs_the_cron_secret(client):
    with mock.patch.dict(os.environ, {"CRON_SECRET": "c"}):
        assert client.post("/api/tick").status_code == 401
        with mock.patch("scheduler.local_now", return_value=at(TOMORROW, 3)):
            assert client.post("/api/tick", headers={"X-Cron-Secret": "c"}).status_code == 200


# ---- "Time to go" ----

def leave_tick(client, now, context):
    with mock.patch.dict(os.environ, KEYS), mock.patch("scheduler.local_now", return_value=now), \
         mock.patch("alerts.build_context", return_value=context) as planned, \
         mock.patch("pywebpush.webpush") as sent:
        body = client.post("/api/tick").get_json()
    # Only the "time to go" pushes; the evening alert goes through the same mock.
    leaving = [json.loads(c.kwargs["data"]) for c in sent.call_args_list]
    return body, planned.call_count, [p for p in leaving if ": leave " in p["title"]]


def test_leave_now_goes_out_once_shortly_before_leaving(client):
    arrive = at(TOMORROW, 9)
    trip = store.save_trip({"label": "Work", "destination": "x", "dest_lat": 37.79, "dest_lon": -122.39,
                            "arrive_at": arrive.isoformat(timespec="minutes")})
    store.set_setting(f"alerted:{trip['id']}:{arrive.isoformat(timespec='minutes')}", "sent last night")
    ctx = {**CONTEXT, "eta_min": 25, "traffic_delta_min": 6,
           "leave_by": at(TOMORROW, 8, 35).isoformat(timespec="minutes")}

    body, planned, sent = leave_tick(client, at(TOMORROW, 7, 0), ctx)        # plans, too early
    assert planned == 1 and body["leave_now"] == 0
    body, planned, sent = leave_tick(client, at(TOMORROW, 7, 10), ctx)       # plan is fresh: no call
    assert planned == 0
    body, planned, sent = leave_tick(client, at(TOMORROW, 8, 20), ctx)       # 15 min before: go
    assert body["leave_now"] == 1
    assert [p["title"] for p in sent] == ["Work: leave in 15 min (8:35 AM)"]
    body, planned, sent = leave_tick(client, at(TOMORROW, 8, 30), ctx)       # already sent
    assert body["leave_now"] == 0 and not sent


def test_no_leave_now_once_it_is_too_late(client):
    store.save_trip({"label": "Gym", "destination": "x", "dest_lat": 37.79, "dest_lon": -122.39,
                     "arrive_at": at(TOMORROW, 9).isoformat(timespec="minutes")})
    ctx = {**CONTEXT, "leave_by": at(TOMORROW, 8, 35).isoformat(timespec="minutes")}
    body, _, sent = leave_tick(client, at(TOMORROW, 8, 50), ctx)
    assert body["leave_now"] == 0 and not sent


def test_leave_now_wording():
    trip = {"id": 1, "label": "Work", "next_at": "x"}
    now = at(TOMORROW, 8, 30)
    transit = {"mode": "transit", "leave_by": at(TOMORROW, 8, 30).isoformat(),
               "transit": {"steps": ["Walk 3 min to Church St", "Take the N Judah toward Ocean Beach from Church St (8:36 AM), 6 stops, get off at Carl St"],
                           "live": ["The N is running 4 min late"]}}
    p = alerts.leave_now_payload(trip, transit, {"risk": "none"}, now)
    assert p["title"] == "Work: leave now (8:30 AM)"
    assert p["body"].startswith("The N is running 4 min late. Take the N Judah")
    # A service notice about another day doesn't lead a "time to go".
    notice = {**transit, "transit": {**transit["transit"],
                                     "live": ["Muni alert: Parade on Wed., 11/4"]}}
    assert alerts.leave_now_payload(trip, notice, {"risk": "none"}, now)["body"].startswith("Take the N Judah")
    drive = {"mode": "drive", "eta_min": 25, "traffic_delta_min": 6,
             "leave_by": at(TOMORROW, 8, 42).isoformat()}
    p = alerts.leave_now_payload(trip, drive, {"risk": "none"}, now)
    assert p["body"] == "25 min drive, 6 min slower than usual."


def test_transit_leave_by_is_comparable_with_the_clock(wednesday):
    # The planner's times carry a UTC offset and the clock's don't; comparing
    # them crashed the first real "leave now" for a transit trip.
    arrive = f"{wednesday}T09:00"
    drawn = alerts.route((-122.4056, 37.7785), (-122.3937, 37.7955), mode="transit", arrive_by=arrive)
    leave = datetime.fromisoformat(drawn["leave_by"])
    assert leave.tzinfo is None and leave < datetime.fromisoformat(arrive)
