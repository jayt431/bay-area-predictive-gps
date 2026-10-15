"""
Push notifications: who can register, where the server will send, and what
happens to a device that's gone. pywebpush is replaced by a fake throughout.
"""

import os
from unittest import mock

import pytest

import push

KEYS = {"VAPID_PUBLIC_KEY": "pub", "VAPID_PRIVATE_KEY": "priv"}


def sub(host="fcm.googleapis.com", tail="abc"):
    return {"endpoint": f"https://{host}/fcm/send/{tail}", "keys": {"p256dh": "p", "auth": "a"}}


@pytest.fixture(autouse=True)
def clean():
    push._save([], "local")
    yield
    push._save([], "local")


@pytest.mark.parametrize("host", ["fcm.googleapis.com", "web.push.apple.com", "updates.push.services.mozilla.com",
                                  "wns2-bl2p.notify.windows.com"])
def test_real_push_services_are_accepted(host):
    assert push.add(sub(host))


@pytest.mark.parametrize("endpoint", ["https://evil.example.com/x", "http://fcm.googleapis.com/x",
                                      "https://fcm.googleapis.com.evil.com/x", "https://169.254.169.254/latest"])
def test_anything_else_is_refused(endpoint):
    # The server POSTs to the endpoint; an arbitrary URL would let it be aimed anywhere.
    assert not push.add({"endpoint": endpoint, "keys": {"p256dh": "p", "auth": "a"}})


def test_same_device_twice_is_one_entry():
    push.add(sub()); push.add(sub())
    assert len(push.subscriptions()) == 1


def test_gone_devices_are_dropped_and_others_still_get_it():
    from pywebpush import WebPushException
    push.add(sub(tail="alive")); push.add(sub(tail="gone"))

    def fake(subscription_info, **kw):
        if subscription_info["endpoint"].endswith("gone"):
            raise WebPushException("gone", response=mock.Mock(status_code=410))
    with mock.patch.dict(os.environ, KEYS), mock.patch("pywebpush.webpush", side_effect=fake):
        result = push.send({"title": "t"})
    assert result == {"sent": 1, "failed": 0, "removed": 1}
    assert [s["endpoint"] for s in push.subscriptions()] == [sub(tail="alive")["endpoint"]]


def test_send_without_keys_says_why():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VAPID_PRIVATE_KEY", None)
        assert "VAPID" in push.send({"title": "t"})["reason"]


def test_subscribe_is_gated(client):
    with mock.patch.dict(os.environ, {"SCHEDULE_PASSCODE": "s3cret"}):
        assert client.post("/api/push/subscribe", json={"subscription": sub()}).status_code == 401
        ok = client.post("/api/push/subscribe", json={"subscription": sub(), "label": "iPhone · Safari"},
                         headers={"X-Schedule-Passcode": "s3cret"})
        assert ok.status_code == 200 and ok.get_json()["devices"] == 1


def test_public_key_and_manifest_are_public(client):
    with mock.patch.dict(os.environ, KEYS):
        assert client.get("/api/push/key").get_json() == {"configured": True, "public_key": "pub"}
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200 and manifest.get_json()["display"] == "standalone"
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["Content-Type"]


def test_alert_run_delivers_by_push(client):
    import scheduler, store
    store.save_trip({"label": "Work", "destination": "x", "dest_lat": 37.79, "dest_lon": -122.39,
                     "days": "MO,TU,WE,TH,FR,SA,SU", "time_of_day": "23:59"})
    push.add(sub())
    context = {"destination": "x", "dest_lat": 37.79, "dest_lon": -122.39, "eta_min": 10,
               "distance_mi": 2, "disruptions": [], "parking": [], "mode": "drive", "leave_by_text": "8:41 AM"}
    with mock.patch.dict(os.environ, KEYS), mock.patch("alerts.build_context", return_value=context), \
         mock.patch("pywebpush.webpush") as sent:
        data = client.post("/api/alerts/run?within_hours=48").get_json()
    delivered = [a for a in data["alerts"] if a.get("delivery")]
    assert delivered and delivered[0]["delivery"]["sent"] and delivered[0]["delivery"]["push"]["sent"] == 1
    payload = sent.call_args.kwargs["data"]
    assert "leave by 8:41 AM" in payload


def test_alert_run_with_no_channel_says_so(client):
    import store
    store.save_trip({"label": "Gym", "destination": "x", "dest_lat": 37.79, "dest_lon": -122.39,
                     "days": "MO,TU,WE,TH,FR,SA,SU", "time_of_day": "23:58"})
    context = {"destination": "x", "dest_lat": 37.79, "dest_lon": -122.39, "eta_min": 10,
               "distance_mi": 2, "disruptions": [], "parking": [], "mode": "drive"}
    with mock.patch("alerts.build_context", return_value=context):
        for key in KEYS:
            os.environ.pop(key, None)
        data = client.post("/api/alerts/run?within_hours=48").get_json()
    gym = [a for a in data["alerts"] if a.get("trip") == "Gym"][0]
    assert gym["delivery"]["sent"] is False and "no channel" in gym["delivery"]["reason"]
