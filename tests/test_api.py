"""
The HTTP surface: input validation, what's public, what's gated.
"""

import os
from unittest import mock

import pytest


def test_health_reports_without_secrets(client):
    data = client.get("/api/health").get_json()
    for key in ("transit_agencies", "transit_data_until", "requests_511", "events"):
        assert key in data
    assert data["transit_agencies"] >= 7


def test_removed_mock_parking_stays_removed(client):
    # It served invented availability and break-in risk.
    assert client.get("/api/parking?lat=37.79&lon=-122.39").status_code == 404


@pytest.mark.parametrize("query", ["", "&leave_at=nope", "&arrive_by=13:00"])
def test_transit_rejects_bad_input(client, query):
    url = "/api/transit?dest_lat=37.7956&dest_lon=-122.3934" + query if query else "/api/transit"
    assert client.get(url).status_code == 400


def test_transit_from_a_chosen_origin(client, wednesday):
    url = (f"/api/transit?dest_lat=37.7956&dest_lon=-122.3934&origin_lat=37.7601&origin_lon=-122.5090"
           f"&leave_at={wednesday}T08:00")
    data = client.get(url).get_json()
    assert data["routes"], data.get("error")


def test_events_rejects_bad_time(client):
    assert client.get("/api/events?from=nope").status_code == 400


def test_places_validate_and_drop_bad_entries(client):
    body = {"home": {"name": "Home", "lat": 37.79, "lon": -122.39},
            "saved": [{"name": "Work", "lat": 37.78, "lon": -122.40},
                      {"name": "Typo", "lat": 40.7, "lon": -74.0},        # New York: out of bounds
                      {"name": "Work", "lat": 37.0, "lon": -122.0}]}      # duplicate name
    saved = client.put("/api/places", json=body).get_json()
    assert [p["name"] for p in saved["saved"]] == ["Work"]
    assert client.get("/api/places").get_json()["home"]["name"] == "Home"


@pytest.mark.parametrize("method,path", [("get", "/api/places"), ("put", "/api/places"), ("get", "/api/schedule")])
def test_personal_endpoints_are_gated(client, method, path):
    with mock.patch.dict(os.environ, {"SCHEDULE_PASSCODE": "s3cret"}):
        assert getattr(client, method)(path, json={}).status_code == 401
        ok = getattr(client, method)(path, json={}, headers={"X-Schedule-Passcode": "s3cret"})
        assert ok.status_code in (200, 400)


def test_public_endpoints_stay_open(client):
    with mock.patch.dict(os.environ, {"SCHEDULE_PASSCODE": "s3cret"}):
        for path in ("/api/config", "/api/health"):
            assert client.get(path).status_code == 200
        # The public config never carries a real home.
        assert client.get("/api/config").get_json()["home"]["label"].startswith("Home · SoMa")
