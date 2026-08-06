"""
Data layer for the Predictive Route Intelligence Agent (Bay Area edition).

This is now a HYBRID layer:
  - Weather  -> LIVE via OpenWeather (needs OPENWEATHER_API_KEY)
  - News     -> LIVE via NewsAPI, scoped to the Bay Area (needs NEWSAPI_KEY)
  - Events   -> MOCKED (Ticketmaster swap documented as a next step)
  - Traffic  -> MOCKED baseline (real: Google Maps Routes API)

Every fetch function takes a `route_id` and resolves the route's coordinates
and corridor internally, so the agent never has to carry latitude/longitude
around. That mirrors how a real system works: you don't ask the language model
to remember coordinates, you look them up from the route.

All routines are anchored to real Bay Area locations so the live weather and
news calls return real, relevant data.
"""

from __future__ import annotations

import math
import os
import random
from datetime import datetime, timedelta

import requests

# ---------------------------------------------------------------------------
# Seeded user routines (real Bay Area geography)
# ---------------------------------------------------------------------------

ROUTINES = {
    "alex_evening_commute": {
        "user": "Alex",
        "label": "Evening commute home",
        "origin": "Financial District, San Francisco",
        "destination": "San Mateo",
        "corridor": ["Financial District", "Mission Bay", "Chase Center", "US-101 South", "San Mateo"],
        "usual_departure": "18:00",
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "route_id": "rt_alex_home",
        "typical_duration_min": 40,
        "lat": 37.7946,   # origin, used for the weather call
        "lon": -122.3999,
        "news_area": "San Francisco",
    },
    "priya_morning_commute": {
        "user": "Priya",
        "label": "Morning commute to work",
        "origin": "Berkeley",
        "destination": "SoMa, San Francisco",
        "corridor": ["Berkeley", "I-80", "Bay Bridge", "SoMa"],
        "usual_departure": "07:30",
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "route_id": "rt_priya_work",
        "typical_duration_min": 35,
        "lat": 37.8715,
        "lon": -122.2730,
        "news_area": "Bay Bridge",
    },
    "sam_weekend_market": {
        "user": "Sam",
        "label": "Weekend farmers market run",
        "origin": "Noe Valley, San Francisco",
        "destination": "Ferry Building Marketplace",
        "corridor": ["Noe Valley", "Market Street", "Embarcadero", "Ferry Building"],
        "usual_departure": "09:30",
        "days": ["Sat", "Sun"],
        "route_id": "rt_sam_market",
        "typical_duration_min": 20,
        "lat": 37.7509,
        "lon": -122.4337,
        "news_area": "San Francisco",
    },
}


def get_routine(route_id: str) -> dict:
    for r in ROUTINES.values():
        if r["route_id"] == route_id:
            return r
    raise KeyError(f"unknown route_id: {route_id}")


# ---------------------------------------------------------------------------
# EVENTS (mocked). Keyed by route so the demo narratives stay stable even as
# the target dates float. Real replacement: Ticketmaster Discovery API.
# ---------------------------------------------------------------------------

_EVENTS_BY_ROUTE = {
    "rt_alex_home": [
        {
            "name": "Major concert at Chase Center",
            "type": "concert",
            "venue": "Chase Center",
            "area": "Chase Center",
            "start_time": "19:30",
            "expected_doors": "18:00",
            "expected_attendance": 18000,
            "distance_from_route_km": 0.5,
            "lat": 37.7680,
            "lon": -122.3874,
        },
        {
            "name": "Neighborhood book fair",
            "type": "community",
            "venue": "San Mateo Library",
            "area": "San Mateo",
            "start_time": "10:00",
            "expected_doors": "10:00",
            "expected_attendance": 300,
            "distance_from_route_km": 1.2,
            "lat": 37.5665,
            "lon": -122.3230,
        },
    ],
    "rt_priya_work": [
        {
            "name": "Weekly farmers market",
            "type": "community",
            "venue": "Downtown Berkeley",
            "area": "Berkeley",
            "start_time": "08:00",
            "expected_doors": "08:00",
            "expected_attendance": 400,
            "distance_from_route_km": 0.6,
            "lat": 37.8703,
            "lon": -122.2725,
        }
    ],
    "rt_sam_market": [
        {
            "name": "Ferry Plaza Farmers Market",
            "type": "community",
            "venue": "Ferry Building",
            "area": "Ferry Building",
            "start_time": "08:00",
            "expected_doors": "08:00",
            "expected_attendance": 500,
            "distance_from_route_km": 0.1,
            "lat": 37.7956,
            "lon": -122.3934,
        }
    ],
}


def _timing_overlaps(event_start: str, departure: str, duration_min: int) -> bool:
    """True if the event start falls within 1 hour of the commute window."""
    from datetime import datetime, timedelta
    fmt = "%H:%M"
    dep = datetime.strptime(departure, fmt)
    window_start = dep - timedelta(hours=1)
    window_end = dep + timedelta(minutes=duration_min) + timedelta(hours=1)
    evt = datetime.strptime(event_start, fmt)
    return window_start <= evt <= window_end


def get_events_near_route(route_id: str, date: str) -> list[dict]:
    """Return mocked events for this route, tagged with corridor proximity and timing."""
    routine = get_routine(route_id)
    corridor = routine["corridor"]
    events = _EVENTS_BY_ROUTE.get(route_id, [])
    out = []
    for e in events:
        on_corridor = e["area"] in corridor or e["distance_from_route_km"] <= 0.7
        timing_overlap = _timing_overlaps(
            e["start_time"], routine["usual_departure"], routine["typical_duration_min"]
        )
        out.append({**e, "date": date, "on_corridor": on_corridor, "timing_overlap": timing_overlap})
    return out


def get_alert_pins(route_id: str, date: str) -> list[dict]:
    """Shape events as map pins with a severity the frontend can color.

    red    = on the corridor AND overlapping the commute window (will affect)
    yellow = only one of the two is true (might affect)
    events matching neither are dropped: they don't belong on the map.
    """
    pins = []
    for e in get_events_near_route(route_id, date):
        both = e["on_corridor"] and e["timing_overlap"]
        either = e["on_corridor"] or e["timing_overlap"]
        if both:
            severity = "red"
        elif either:
            severity = "yellow"
        else:
            continue
        pins.append({
            "name": e["name"],
            "lat": e["lat"],
            "lon": e["lon"],
            "severity": severity,
            "start_time": e["start_time"],
            "reason": _pin_reason(e, severity),
        })
    return pins


def _pin_reason(event: dict, severity: str) -> str:
    if severity == "red":
        return f"On your route and starts near departure ({event['start_time']})."
    if event["on_corridor"]:
        return f"On your route but starts at {event['start_time']}, off your window."
    return f"Near departure ({event['start_time']}) but off your usual route."


# ---------------------------------------------------------------------------
# SINGLE-USER GPS MODEL (new)
#
# One user with a fixed home base, plus a Bay Area pool of possible
# disruptions. The frontend draws a route from HOME to a chosen destination
# and surfaces the disruptions that fall near that route. This replaces the
# per-persona model in the UI; the ROUTINES/events above are kept only for
# the agent and the /map-test page.
# ---------------------------------------------------------------------------

HOME = {"label": "Home · SoMa, San Francisco", "lat": 37.7785, "lon": -122.4056}

_DISRUPTIONS = [
    {"id": "d1", "name": "Concert at Chase Center", "type": "event",
     "lat": 37.7680, "lon": -122.3874, "time": "Doors 6:00 PM",
     "note": "18,000 attendees; heavy load around Mission Bay."},
    {"id": "d2", "name": "Giants game at Oracle Park", "type": "event",
     "lat": 37.7786, "lon": -122.3893, "time": "First pitch 7:15 PM",
     "note": "Congestion along the Embarcadero and 3rd St."},
    {"id": "d3", "name": "Bay Bridge on-ramp construction", "type": "construction",
     "lat": 37.7918, "lon": -122.3908, "time": "All day",
     "note": "Lane closures near the Bay Bridge approach."},
    {"id": "d4", "name": "Protest march, Civic Center", "type": "protest",
     "lat": 37.7797, "lon": -122.4181, "time": "Starts 4:00 PM",
     "note": "Rolling street closures around Market & Van Ness."},
    {"id": "d5", "name": "Crash on US-101 NB", "type": "incident",
     "lat": 37.7666, "lon": -122.4064, "time": "Reported 20 min ago",
     "note": "Two lanes blocked; residual delays."},
    {"id": "d6", "name": "Ferry Plaza Farmers Market", "type": "market",
     "lat": 37.7956, "lon": -122.3934, "time": "8:00 AM - 2:00 PM",
     "note": "Small, recurring; minor foot traffic on the Embarcadero."},
    {"id": "d7", "name": "Flooding on the Embarcadero", "type": "weather",
     "lat": 37.8000, "lon": -122.3980, "time": "High tide ~5:30 PM",
     "note": "King-tide ponding near Pier 7."},
]


def get_disruptions() -> list[dict]:
    """Return the Bay Area pool of possible disruptions (mocked for now)."""
    return [dict(d) for d in _DISRUPTIONS]


# ---------------------------------------------------------------------------
# PARKING (mocked, phase 1)
#
# Generates a few parking zones around whatever destination the user routed
# to. Deterministic: the same destination always yields the same zones, so
# the demo is stable. Phase 2 replaces this with real SF open data (street
# sweeping schedules, garage occupancy, SFPD break-in incidents).
# ---------------------------------------------------------------------------

_CLEANING_TIMES = ["Tue 8-10 AM", "Wed 12-2 PM", "Thu 9-11 AM", "Mon 2-4 PM", "None posted this week"]
_DIR_LABELS = {"N": "North", "E": "East", "S": "South", "W": "West"}
_DIR_ANGLES = [("N", 0), ("E", 90), ("S", 180), ("W", 270)]


def _avail_note(avail: str, ptype: str) -> str:
    if ptype == "garage":
        return {"easy": "Garage, plenty of space", "moderate": "Garage, filling up",
                "hard": "Garage, nearly full"}[avail]
    return {"easy": "Street spots usually open", "moderate": "Street parking is tight",
            "hard": "Street parking usually full"}[avail]


_METERS_URL = "https://data.sfgov.org/resource/8vzz-qzz9.json"


def _predict_avail(name: str, count: int) -> str:
    """Stable predicted availability (placeholder — real curb availability
    isn't published). Deterministic per street so the demo stays stable."""
    r = (sum(ord(c) for c in name) * 7 + count) % 10
    return "easy" if r < 3 else "moderate" if r < 7 else "hard"


def _predict_risk(name: str) -> str:
    return "elevated" if sum(ord(c) for c in name) % 3 == 0 else "low"


def get_metered_streets(lat: float, lon: float, radius: int = 350, max_streets: int = 6) -> dict:
    """Real metered street parking near a point, from SF's DataSF open data.

    Locations are real (every SFMTA meter); availability is predicted, since no
    live curb-availability feed exists. Meters are grouped by street.
    """
    params = {
        "$where": f"within_circle(shape,{lat},{lon},{radius}) AND on_offstreet_type='ON'",
        "$select": "street_name,latitude,longitude",
        "$limit": 800,
    }
    try:
        resp = requests.get(_METERS_URL, params=params, timeout=15)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        return {"error": f"meter fetch failed: {exc}", "streets": []}

    groups: dict[str, list[tuple[float, float]]] = {}
    for r in rows:
        try:
            la, lo = float(r["latitude"]), float(r["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        groups.setdefault(r.get("street_name") or "Unknown", []).append((lo, la))

    streets = []
    for name, pts in groups.items():
        clon = sum(p[0] for p in pts) / len(pts)
        clat = sum(p[1] for p in pts) / len(pts)
        avail = _predict_avail(name, len(pts))
        streets.append({
            "street": name.title(),
            "count": len(pts),
            "lat": round(clat, 6),
            "lon": round(clon, 6),
            "availability": avail,
            "risk": _predict_risk(name),
            "note": "Metered — paid street parking",
            "meters": [[round(lo, 6), round(la, 6)] for lo, la in pts[:40]],
        })
    streets.sort(key=lambda s: s["count"], reverse=True)
    return {"streets": streets[:max_streets]}


def get_parking(lat: float, lon: float) -> list[dict]:
    """Return mocked parking zones around a destination point.

    Availability drives the zone color; risk == 'elevated' flags a possible
    break-in hotspot the frontend marks with a caution icon.
    """
    seed = int(round(lat, 3) * 1000) * 100000 + int(round(lon, 3) * 1000)
    rng = random.Random(seed)
    dirs = _DIR_ANGLES[:]
    rng.shuffle(dirs)

    zones = []
    for i, (dname, ang) in enumerate(dirs):
        dist_m = rng.uniform(180, 360)
        dlat = (dist_m * math.cos(math.radians(ang))) / 111111.0
        dlon = (dist_m * math.sin(math.radians(ang))) / (111111.0 * math.cos(math.radians(lat)))
        avail = rng.choices(["easy", "moderate", "hard"], weights=[3, 4, 3])[0]
        risk = rng.choices(["low", "elevated"], weights=[4, 2])[0]
        ptype = "garage" if (i == 0 and rng.random() < 0.4) else "street"
        cleaning = "N/A (garage)" if ptype == "garage" else rng.choice(_CLEANING_TIMES)
        zones.append({
            "id": f"pz{i}",
            "name": f"{_DIR_LABELS[dname]} of destination",
            "lat": round(lat + dlat, 6),
            "lon": round(lon + dlon, 6),
            "type": ptype,
            "availability": avail,
            "risk": risk,
            "cleaning": cleaning,
            "note": _avail_note(avail, ptype),
        })
    return zones


# ---------------------------------------------------------------------------
# TRAFFIC baseline (mocked). Real: Google Maps Routes API typical traffic.
# ---------------------------------------------------------------------------

_TRAFFIC_BASELINE = {
    "rt_alex_home": {"18:00": "heavy", "17:00": "moderate", "19:00": "heavy"},
    "rt_priya_work": {"07:30": "moderate", "07:00": "light", "08:30": "heavy"},
    "rt_sam_market": {"09:30": "light", "10:00": "light"},
}


def get_traffic_baseline(route_id: str) -> dict:
    routine = get_routine(route_id)
    dep = routine["usual_departure"]
    return {
        "route_id": route_id,
        "departure_time": dep,
        "typical_congestion": _TRAFFIC_BASELINE.get(route_id, {}).get(dep, "unknown"),
    }


# ---------------------------------------------------------------------------
# WEATHER (LIVE, OpenWeather 5-day / 3-hour forecast, free tier)
# ---------------------------------------------------------------------------

_OW_URL = "https://api.openweathermap.org/data/2.5/forecast"


def get_weather_forecast(route_id: str, date: str) -> dict:
    """Live forecast for the route origin, filtered to the commute window.

    Uses the free /data/2.5/forecast endpoint (5 days, 3-hour steps). The
    paid One Call API is not required. Timestamps are converted to local time
    using the timezone offset the API returns.
    """
    key = os.environ.get("OPENWEATHER_API_KEY")
    if not key:
        return {"date": date, "error": "OPENWEATHER_API_KEY not set", "alerts": []}

    routine = get_routine(route_id)
    dep_hour = int(routine["usual_departure"].split(":")[0])
    win_start = dep_hour - 1
    win_end = dep_hour + (routine["typical_duration_min"] // 60) + 1

    try:
        resp = requests.get(
            _OW_URL,
            params={"lat": routine["lat"], "lon": routine["lon"],
                    "appid": key, "units": "imperial"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return {"date": date, "error": f"weather fetch failed: {exc}", "alerts": []}

    tz_offset = data.get("city", {}).get("timezone", 0)
    steps = []
    for item in data.get("list", []):
        local = datetime.utcfromtimestamp(item["dt"] + tz_offset)
        if local.strftime("%Y-%m-%d") == date and win_start <= local.hour <= win_end:
            steps.append({
                "time": local.strftime("%H:%M"),
                "conditions": item["weather"][0]["main"],
                "description": item["weather"][0]["description"],
                "temp_f": round(item["main"]["temp"]),
                "wind_mph": round(item["wind"]["speed"]),
                "precip_probability": item.get("pop", 0),
            })

    if not steps:
        return {
            "date": date, "area": routine["origin"], "window": f"{win_start:02d}:00-{win_end:02d}:00",
            "summary": "No forecast for this window (beyond the 5-day range or no matching step).",
            "alerts": [],
        }

    max_pop = max(s["precip_probability"] for s in steps)
    max_wind = max(s["wind_mph"] for s in steps)
    worst = max(steps, key=lambda s: (s["precip_probability"], s["wind_mph"]))

    alerts = []
    if max_pop >= 0.6:
        alerts.append(f"High chance of rain ({int(max_pop*100)}%) during the commute window.")
    if max_wind >= 30:
        alerts.append(f"Strong winds up to {max_wind} mph during the window.")

    return {
        "date": date,
        "area": routine["origin"],
        "window": f"{win_start:02d}:00-{win_end:02d}:00 local",
        "summary": worst["description"],
        "temp_f": worst["temp_f"],
        "max_precip_probability": round(max_pop, 2),
        "max_wind_mph": max_wind,
        "alerts": alerts,
        "hourly": steps,
    }


# ---------------------------------------------------------------------------
# NEWS (LIVE, NewsAPI /v2/everything, free developer tier, Bay Area scoped)
# ---------------------------------------------------------------------------

_NEWS_URL = "https://newsapi.org/v2/everything"


def get_local_news(route_id: str, date: str) -> dict:
    """Recent Bay Area news that could signal a route disruption.

    News is inherently about what has recently been reported, not a future
    date, so `date` is contextual only. The query is scoped to the Bay Area
    and to this route's corridor, plus disruption keywords.
    """
    key = os.environ.get("NEWSAPI_KEY")
    if not key:
        return {"error": "NEWSAPI_KEY not set", "articles": []}

    routine = get_routine(route_id)
    area = routine["news_area"]
    # Bay Area scope + this corridor + disruption signals.
    disruption = "traffic OR closure OR protest OR construction OR crash OR delay OR flooding"
    query = f'("{area}" OR "Bay Area") AND ({disruption})'
    since = (datetime.utcnow() - timedelta(days=5)).strftime("%Y-%m-%d")

    try:
        resp = requests.get(
            _NEWS_URL,
            params={"q": query, "language": "en", "sortBy": "publishedAt",
                    "from": since, "pageSize": 5, "apiKey": key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return {"error": f"news fetch failed: {exc}", "articles": []}

    articles = [
        {
            "headline": a.get("title"),
            "source": a.get("source", {}).get("name"),
            "published": a.get("publishedAt"),
            "description": a.get("description"),
            "url": a.get("url"),
        }
        for a in data.get("articles", [])
    ]
    return {"query_area": area, "as_of": date, "articles": articles}
