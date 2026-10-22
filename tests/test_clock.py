"""
The clock (POST /api/tick): each alert at its time, once, and the calendar
re-read every few hours. Time is frozen by replacing scheduler.local_now;
routing and delivery are fakes.
"""

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
def clean():
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
