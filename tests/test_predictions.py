"""
The prediction log: the forecast from each delivered alert, what was actually
happening at departure, and your verdict — the evidence for whether the
forecasts are any good. Time is frozen; routing and delivery are fakes.
"""

import os
from datetime import datetime, timedelta
from unittest import mock

import pytest

import push
import store

KEYS = {"VAPID_PUBLIC_KEY": "pub", "VAPID_PRIVATE_KEY": "priv"}
TOMORROW = (datetime.now() + timedelta(days=1)).date()


def at(hour, minute=0, day=TOMORROW):
    return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)


def context(eta, mode="drive", **extra):
    return {"destination": "x", "dest_lat": 37.79, "dest_lon": -122.39, "eta_min": eta,
            "distance_mi": 5, "disruptions": [], "parking": [], "mode": mode,
            "leave_by": at(8, 35).isoformat(timespec="minutes"), **extra}


@pytest.fixture(autouse=True)
def clean(client):                       # client first: it creates the tables
    for trip in store.list_trips():
        store.delete_trip(trip["id"])
    with store.connection() as conn:     # other test files leave forecasts behind
        conn.cursor().execute("DELETE FROM predictions")
        conn.commit()
    push._save([{"endpoint": "https://fcm.googleapis.com/fcm/send/x",
                 "keys": {"p256dh": "p", "auth": "a"}}], "local")
    yield
    push._save([], "local")


@pytest.fixture
def work():
    trip = store.save_trip({"label": "Work", "destination": "x", "dest_lat": 37.79, "dest_lon": -122.39,
                            "arrive_at": at(9).isoformat(timespec="minutes")})
    return f"{trip['id']}:{at(9).isoformat(timespec='minutes')}"


def tick(client, now, ctx):
    with mock.patch.dict(os.environ, KEYS), mock.patch("scheduler.local_now", return_value=now), \
         mock.patch("alerts.build_context", return_value=ctx), mock.patch("pywebpush.webpush"):
        return client.post("/api/tick").get_json()


def test_the_delivered_forecast_is_recorded_once(client, work):
    tick(client, at(20, 5, TOMORROW - timedelta(days=1)), context(25, traffic_delta_min=2))
    row = store.get_prediction(work)
    assert row["predicted_min"] == 25 and row["risk"] == "none" and row["leave_by"] == at(8, 35).isoformat(timespec="minutes")
    # A later alert for the same trip (the "leave now") doesn't replace the forecast being judged.
    tick(client, at(8, 20), context(40))
    assert store.get_prediction(work)["predicted_min"] == 25
    assert store.get_prediction(work)["leave_now_at"] == at(8, 20).isoformat(timespec="minutes")


def test_actual_is_measured_at_departure(client, work):
    tick(client, at(20, 5, TOMORROW - timedelta(days=1)), context(25))
    assert tick(client, at(8, 25), context(27))["measured"] == 0        # not yet left
    assert tick(client, at(8, 36), context(31))["measured"] == 1        # just after leave-by
    assert store.get_prediction(work)["actual_min"] == 31
    assert tick(client, at(8, 46), context(45))["measured"] == 0        # once only


def test_transit_actual_includes_the_live_delay(client, work):
    tick(client, at(20, 5, TOMORROW - timedelta(days=1)), context(25, mode="transit"))
    tick(client, at(8, 36), context(25, mode="transit", live_delay_min=7))
    assert store.get_prediction(work)["actual_min"] == 32


def test_rating_feeds_the_scorecard(client, work):
    tick(client, at(20, 5, TOMORROW - timedelta(days=1)), context(25))
    tick(client, at(8, 36), context(28))
    assert client.post(f"/api/predictions/{work}", json={"verdict": "as_predicted", "changed_plans": True}).status_code == 200
    with mock.patch("scheduler.local_now", return_value=at(10)):
        score = client.get("/api/predictions").get_json()["score"]
    assert score == {"trips": 1, "measured": 1, "time_within_5": 1, "rated": 1, "as_predicted": 1,
                     "warnings": 0, "false_alarms": 0, "changed_plans": 1}


def test_a_warning_rated_better_is_a_false_alarm(client, work):
    crash = {"name": "Crash", "type": "incident", "severity": "red", "reason": "on route"}
    tick(client, at(20, 5, TOMORROW - timedelta(days=1)), context(25, disruptions=[crash]))
    client.post(f"/api/predictions/{work}", json={"verdict": "better"})
    with mock.patch("scheduler.local_now", return_value=at(10)):
        score = client.get("/api/predictions").get_json()["score"]
    assert score["warnings"] == 1 and score["false_alarms"] == 1


def test_bad_verdicts_and_unknown_trips_are_refused(client, work):
    tick(client, at(20, 5, TOMORROW - timedelta(days=1)), context(25))
    assert client.post(f"/api/predictions/{work}", json={"verdict": "great"}).status_code == 400
    assert client.post("/api/predictions/nope:2026-01-01T09:00", json={"verdict": "worse"}).status_code == 404


def test_the_log_is_behind_the_passcode(client):
    with mock.patch.dict(os.environ, {"SCHEDULE_PASSCODE": "s3cret"}):
        assert client.get("/api/predictions").status_code == 401
        assert client.post("/api/predictions/x", json={"verdict": "worse"}).status_code == 401
