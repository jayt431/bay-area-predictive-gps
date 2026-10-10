"""
Public-transit directions from the Google Routes API.

Mapbox, which draws every other route here, has no transit profile, so transit
comes from Google. The request runs server-side: the key never reaches the
browser, and the scheduled-alert job can plan a transit trip with no browser
open.

Google returns one step per walking turn and one per transit ride. That is the
right shape for turn-by-turn, the wrong one for "take the N, get off at
Embarcadero" — so consecutive walking steps are merged into a single walk, and
each ride keeps what a rider needs: the line, which way it is heading, where to
board, where to get off, and how many stops that is.

Cost guard: unlike Mapbox, every request is billed, and the map is a public
demo. Identical requests within `_CACHE_TTL_S` are served from memory, and a
daily ceiling (`TRANSIT_DAILY_LIMIT`) stops runaway spend even if a Google
Cloud quota was never set. Neither is a substitute for setting that quota.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from time import monotonic

import requests
from dateutil import tz

_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
_PACIFIC = tz.gettz("America/Los_Angeles")

# Only what is used below. Google bills by the fields requested, and a field
# mask is mandatory.
_FIELD_MASK = ",".join([
    "routes.duration",
    "routes.distanceMeters",
    "routes.legs.steps.travelMode",
    "routes.legs.steps.distanceMeters",
    "routes.legs.steps.staticDuration",
    "routes.legs.steps.polyline",
    "routes.legs.steps.transitDetails",
])

_CACHE_TTL_S = 120
_cache: dict = {}
_usage = {"day": None, "count": 0}

# Vehicle types worth a friendlier word than Google's enum.
_VEHICLE_WORDS = {
    "BUS": "Bus", "INTERCITY_BUS": "Bus", "TROLLEYBUS": "Bus",
    "TRAM": "Streetcar", "LIGHT_RAIL": "Light rail", "CABLE_CAR": "Cable car",
    "SUBWAY": "Train", "HEAVY_RAIL": "Train", "COMMUTER_TRAIN": "Train",
    "RAIL": "Train", "HIGH_SPEED_TRAIN": "Train", "LONG_DISTANCE_TRAIN": "Train",
    "METRO_RAIL": "Train", "MONORAIL": "Train", "FERRY": "Ferry",
}


def available() -> bool:
    return bool(os.environ.get("GOOGLE_MAPS_API_KEY"))


def _seconds(duration: str | None) -> int:
    """Google durations are strings like "754s"."""
    try:
        return int(float((duration or "0s").rstrip("s")))
    except ValueError:
        return 0


def _parse_time(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _clock(moment: datetime | None) -> str | None:
    if not moment:
        return None
    local = moment.astimezone(_PACIFIC)
    return local.strftime("%I:%M %p").lstrip("0")


def _to_utc_stamp(local_iso: str) -> str | None:
    """A trip's naive Pacific time ("2026-10-20T08:30") as an RFC 3339 UTC stamp."""
    try:
        naive = datetime.fromisoformat(local_iso)
    except ValueError:
        return None
    aware = naive.replace(tzinfo=_PACIFIC) if naive.tzinfo is None else naive
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coords(step: dict) -> list:
    line = (step.get("polyline") or {}).get("geoJsonLinestring") or {}
    return line.get("coordinates") or []


def normalize(route: dict) -> dict:
    """One Google route as a rider reads it: walk, ride, walk."""
    steps = [s for leg in route.get("legs") or [] for s in leg.get("steps") or []]
    out: list[dict] = []

    for step in steps:
        coords = _coords(step)
        if step.get("travelMode") == "TRANSIT" and step.get("transitDetails"):
            td = step["transitDetails"]
            line = td.get("transitLine") or {}
            vehicle = line.get("vehicle") or {}
            stops = td.get("stopDetails") or {}
            depart = _parse_time(stops.get("departureTime"))
            arrive = _parse_time(stops.get("arrivalTime"))
            agencies = line.get("agencies") or []
            vtype = vehicle.get("type") or ""
            out.append({
                "kind": "transit",
                # Short name only ("N", "38"). BART lines have none; the badge
                # then falls back to the agency and the full name goes in the text.
                "line": line.get("nameShort") or "",
                "line_name": line.get("name") or "",
                "color": line.get("color") or None,
                "text_color": line.get("textColor") or None,
                "vehicle": _VEHICLE_WORDS.get(vtype) or (vehicle.get("name") or {}).get("text") or "Transit",
                "vehicle_type": vtype,
                "agency": agencies[0].get("name") if agencies else None,
                "headsign": td.get("headsign"),
                "from_stop": (stops.get("departureStop") or {}).get("name"),
                "to_stop": (stops.get("arrivalStop") or {}).get("name"),
                "stops": td.get("stopCount"),
                "depart": depart.isoformat() if depart else None,
                "arrive": arrive.isoformat() if arrive else None,
                "depart_text": _clock(depart),
                "arrive_text": _clock(arrive),
                "duration_s": _seconds(step.get("staticDuration")),
                "distance_m": step.get("distanceMeters") or 0,
                "coords": coords,
            })
        else:
            # Walking (and the rare non-transit step): fold into the walk so far.
            if out and out[-1]["kind"] == "walk":
                walk = out[-1]
                walk["duration_s"] += _seconds(step.get("staticDuration"))
                walk["distance_m"] += step.get("distanceMeters") or 0
                walk["coords"] += coords[1:] if walk["coords"] else coords
            else:
                out.append({"kind": "walk",
                            "duration_s": _seconds(step.get("staticDuration")),
                            "distance_m": step.get("distanceMeters") or 0,
                            "coords": list(coords)})

    # Say where each walk goes: to the next boarding stop, or to the destination.
    for i, seg in enumerate(out):
        if seg["kind"] == "walk":
            following = out[i + 1] if i + 1 < len(out) else None
            seg["to"] = following["from_stop"] if following else None

    rides = [s for s in out if s["kind"] == "transit"]
    duration = _seconds(route.get("duration"))
    leave = arrive = None
    if rides and rides[0]["depart"]:
        # Leave early enough to walk to the first stop.
        walk_before = sum(s["duration_s"] for s in out[:out.index(rides[0])])
        leave = datetime.fromisoformat(rides[0]["depart"]) - timedelta(seconds=walk_before)
    if rides and rides[-1]["arrive"]:
        walk_after = sum(s["duration_s"] for s in out[out.index(rides[-1]) + 1:])
        arrive = datetime.fromisoformat(rides[-1]["arrive"]) + timedelta(seconds=walk_after)

    return {
        "duration_s": duration or sum(s["duration_s"] for s in out),
        "distance_m": route.get("distanceMeters") or sum(s["distance_m"] for s in out),
        "leave_at": leave.isoformat() if leave else None,
        "leave_text": _clock(leave),
        "arrive_at": arrive.isoformat() if arrive else None,
        "arrive_text": _clock(arrive),
        "lines": [s["line"] or s["agency"] or s["line_name"] for s in rides],
        "segments": out,
    }


def _within_daily_limit() -> bool:
    today = date.today()
    if _usage["day"] != today:
        _usage["day"], _usage["count"] = today, 0
    limit = int(os.environ.get("TRANSIT_DAILY_LIMIT", "500"))
    if _usage["count"] >= limit:
        return False
    _usage["count"] += 1
    return True


def plan(origin: tuple[float, float], destination: tuple[float, float],
         arrive_by: str | None = None, alternatives: bool = True) -> dict:
    """Transit options between two (lat, lon) points.

    `arrive_by` is a naive Pacific time; without it the trip leaves now.
    Always returns a dict: `routes` (possibly empty) and, on failure, an
    `error` that says why — a missing key, the daily ceiling, or Google's own
    message — rather than an empty answer that looks like "no transit here".
    """
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        return {"routes": [], "error": "GOOGLE_MAPS_API_KEY not set"}

    cache_key = (round(origin[0], 4), round(origin[1], 4),
                 round(destination[0], 4), round(destination[1], 4), arrive_by, alternatives)
    hit = _cache.get(cache_key)
    if hit and monotonic() - hit[0] < _CACHE_TTL_S:
        return hit[1]

    if not _within_daily_limit():
        return {"routes": [], "error": "daily transit lookup limit reached"}

    body = {
        "origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
        "destination": {"location": {"latLng": {"latitude": destination[0], "longitude": destination[1]}}},
        "travelMode": "TRANSIT",
        "polylineEncoding": "GEO_JSON_LINESTRING",
        "computeAlternativeRoutes": alternatives,
    }
    if arrive_by:
        stamp = _to_utc_stamp(arrive_by)
        if stamp:
            body["arrivalTime"] = stamp

    try:
        resp = requests.post(_URL, json=body, timeout=20, headers={
            "X-Goog-Api-Key": key, "X-Goog-FieldMask": _FIELD_MASK})
    except requests.RequestException as exc:
        return {"routes": [], "error": f"transit request failed: {exc.__class__.__name__}"}
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not resp.ok:
        message = (data.get("error") or {}).get("message") or resp.reason
        return {"routes": [], "error": f"Google Routes rejected it ({resp.status_code}): {message[:300]}"}

    routes = [normalize(r) for r in data.get("routes") or []]
    result = {"routes": routes} if routes else {"routes": [], "error": "no transit route found"}
    _cache[cache_key] = (monotonic(), result)
    return result
