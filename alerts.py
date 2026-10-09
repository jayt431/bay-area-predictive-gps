"""
Pre-trip alerts for scheduled trips, computed without a browser.

The map builds a trip in the browser: Mapbox draws the route, Turf measures
which disruptions fall near it, and the result is POSTed to /api/brief. A
scheduled alert has no browser, so this module does the same work server-side —
route from Mapbox Directions, disruption matching by point-to-polyline distance
— and then reuses the exact same brief path the UI uses.

Delivery degrades like every other source here: with `RESEND_API_KEY` the alert
is emailed, without one it is returned and logged so the feature is testable
with no signup.
"""

from __future__ import annotations

import math
import os
from datetime import datetime

import requests

import mock_data

_DIRECTIONS_URL = "https://api.mapbox.com/directions/v5/mapbox/{}/{},{};{},{}"

# Travel modes, keyed by the name stored on a trip, mapped to the Mapbox
# Directions profile that routes them. Transit is absent on purpose: Mapbox has
# no transit profile, so it needs a different provider.
PROFILES = {"drive": "driving", "walk": "walking", "bike": "cycling"}
MODE_NOUN = {"drive": "Drive", "walk": "Walk", "bike": "Bike ride"}


def normalize_mode(mode: str | None) -> str:
    """Any unknown or missing mode is a drive, which is what every trip was
    before modes existed."""
    return mode if mode in PROFILES else "drive"
_RESEND_URL = "https://api.resend.com/emails"

# Same thresholds the map uses, so an emailed alert and the on-screen one agree.
NEAR_RED_KM = 0.5
NEAR_YELLOW_KM = 2.0
_EARTH_RADIUS_KM = 6371.0088


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lon, lat) points."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def _point_to_segment_km(p, a, b) -> float:
    """Distance from a point to a segment, in km.

    Projects in a local flat plane — over the length of one route segment the
    curvature error is far below the 0.5 km threshold this feeds.
    """
    lat_scale = math.cos(math.radians(p[1])) or 1e-9
    px, py = p[0] * lat_scale, p[1]
    ax, ay = a[0] * lat_scale, a[1]
    bx, by = b[0] * lat_scale, b[1]
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return _haversine_km(p, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    closest = (ax + t * dx, ay + t * dy)
    return _haversine_km(p, (closest[0] / lat_scale, closest[1]))


def point_to_line_km(point, line) -> float:
    """Shortest distance from a point to a polyline, in km."""
    if not line:
        return float("inf")
    if len(line) == 1:
        return _haversine_km(point, line[0])
    return min(_point_to_segment_km(point, line[i], line[i + 1])
               for i in range(len(line) - 1))


def route(origin: tuple[float, float], destination: tuple[float, float],
          token: str | None = None, mode: str = "drive") -> dict | None:
    """Route from Mapbox for one travel mode. Coordinates are (lon, lat)."""
    token = token or os.environ.get("MAPBOX_TOKEN", "")
    if not token:
        return None
    try:
        resp = requests.get(
            _DIRECTIONS_URL.format(PROFILES[normalize_mode(mode)],
                                   origin[0], origin[1], destination[0], destination[1]),
            params={"access_token": token, "geometries": "geojson", "overview": "full"},
            timeout=20,
        )
        resp.raise_for_status()
        routes = resp.json().get("routes") or []
    except Exception:
        return None
    if not routes:
        return None
    best = routes[0]
    return {
        "coords": [(c[0], c[1]) for c in best["geometry"]["coordinates"]],
        "eta_min": round(best["duration"] / 60),
        "distance_mi": round(best["distance"] / 1609.34, 1),
    }


def build_context(trip: dict) -> dict | None:
    """Assemble the same trip context the browser POSTs to /api/brief."""
    home = mock_data.HOME
    mode = normalize_mode(trip.get("mode"))
    drawn = route((home["lon"], home["lat"]), (trip["dest_lon"], trip["dest_lat"]), mode=mode)
    if not drawn:
        return None

    on_route = []
    for disruption in mock_data.get_disruptions():
        km = point_to_line_km((disruption["lon"], disruption["lat"]), drawn["coords"])
        severity = "red" if km <= NEAR_RED_KM else "yellow" if km <= NEAR_YELLOW_KM else None
        if not severity:
            continue
        on_route.append({
            "name": disruption["name"],
            "severity": severity,
            "reason": f"{disruption.get('note', '')} ({disruption.get('time', '')} · {km:.1f} km from route)",
        })

    # Parking only matters to a driver, so the lookup is skipped otherwise.
    parking = (mock_data.get_metered_streets(trip["dest_lat"], trip["dest_lon"])
               if mode == "drive" else {})
    return {
        "destination": trip.get("destination") or trip.get("label"),
        "mode": mode,
        "dest_lat": trip["dest_lat"],
        "dest_lon": trip["dest_lon"],
        "eta_min": drawn["eta_min"],
        "distance_mi": drawn["distance_mi"],
        "disruptions": on_route,
        "parking": [{"name": s["street"], "count": s["count"]}
                    for s in (parking.get("streets") or [])[:5]],
    }


def format_email(trip: dict, alert: dict, context: dict) -> tuple[str, str]:
    """Subject and plain-text body for one scheduled trip."""
    when = trip.get("next_at", "")
    pretty = when.replace("T", " at ") if when else "soon"
    risk = (alert.get("risk") or "none").upper()
    subject = f"[{risk}] {trip.get('label') or 'Trip'} — {pretty}"

    lines = [
        alert.get("headline", ""),
        "",
        f"Trip:        {trip.get('label')} → {context.get('destination')}",
        f"Arriving:    {pretty}",
        f"{MODE_NOUN[normalize_mode(context.get('mode'))] + ':':<13}"
        f"{context.get('eta_min')} min, {context.get('distance_mi')} mi from home",
        "",
        alert.get("why", ""),
        "",
        f"Recommendation: {alert.get('recommendation', 'No action needed.')}",
    ]
    if context.get("disruptions"):
        lines += ["", "On your route:"]
        lines += [f"  [{d['severity'].upper()}] {d['name']}" for d in context["disruptions"]]
    lines += ["", "— Bay Area Predictive GPS"]
    return subject, "\n".join(lines)


def send_email(to_address: str, subject: str, body: str) -> dict:
    """Send via Resend. Without a key the alert is reported, not sent."""
    key = os.environ.get("RESEND_API_KEY")
    sender = os.environ.get("ALERT_FROM_EMAIL", "onboarding@resend.dev")
    if not key:
        return {"sent": False, "reason": "RESEND_API_KEY not set"}
    if not to_address:
        return {"sent": False, "reason": "no alert email configured"}
    try:
        resp = requests.post(
            _RESEND_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"from": sender, "to": [to_address], "subject": subject, "text": body},
            timeout=20,
        )
    except Exception as exc:
        return {"sent": False, "reason": f"send failed: {exc}"}
    if not resp.ok:
        # Resend explains a rejection in the body (unverified sender, a test
        # sender writing to someone other than the account owner, a revoked
        # key); the status code alone does not say which.
        try:
            detail = resp.json().get("message") or resp.text
        except ValueError:
            detail = resp.text
        return {"sent": False, "reason": f"Resend rejected it ({resp.status_code}): {detail[:300]}"}
    return {"sent": True}
