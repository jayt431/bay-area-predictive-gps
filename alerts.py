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
from datetime import datetime, time, timedelta

import requests

import events
import mock_data
import places
import transit

_DIRECTIONS_URL = "https://api.mapbox.com/directions/v5/mapbox/{}/{},{};{},{}"

# Travel modes, keyed by the name stored on a trip. Three route through a
# Mapbox Directions profile; transit has none there and goes through our own
# planner over 511 schedule data (transit.py) instead.
# Driving uses the traffic-aware profile: live traffic now, predicted traffic
# for a future departure, and a "typical" duration to compare against.
PROFILES = {"drive": "driving-traffic", "walk": "walking", "bike": "cycling"}
MODES = (*PROFILES, "transit")
MODE_NOUN = {"drive": "Drive", "walk": "Walk", "bike": "Bike ride", "transit": "Transit"}


def normalize_mode(mode: str | None) -> str:
    """Any unknown or missing mode is a drive, which is what every trip was
    before modes existed."""
    return mode if mode in MODES else "drive"
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
          token: str | None = None, mode: str = "drive",
          arrive_by: str | None = None) -> dict | None:
    """Route for one travel mode. Coordinates are (lon, lat).

    With `arrive_by` (a naive Pacific time), transit and driving both answer
    "when do I have to leave?". Transit plans backwards from the arrival.
    Driving asks for predicted traffic at a departure time: Mapbox's traffic
    profile accepts `depart_at` but ignores `arrive_by`, so it guesses a
    departure 30 minutes early, then asks again leaving that many minutes
    before arrival — which settles on the traffic you'll actually meet.
    Walking and biking have no traffic and ignore the time.
    """
    if normalize_mode(mode) == "transit":
        planned = transit.plan((origin[1], origin[0]), (destination[1], destination[0]),
                               arrive_by=arrive_by, alternatives=False)
        if not planned["routes"]:
            return None
        best = planned["routes"][0]
        return {
            "coords": [tuple(c) for seg in best["segments"] for c in seg["coords"]],
            # Street incidents can't delay a train underground or a ferry, so
            # only the walking, bus and streetcar parts are matched — each as
            # its own line, so no false straight line spans the rail section.
            "street_lines": [[tuple(c) for c in seg["coords"]] for seg in best["segments"]
                             if len(seg["coords"]) > 1
                             and not (seg["kind"] == "transit" and seg.get("vehicle") in ("Train", "Ferry"))],
            "eta_min": round(best["duration_s"] / 60),
            "distance_mi": round(best["distance_m"] / 1609.34, 1),
            "transit": best,
        }
    token = token or os.environ.get("MAPBOX_TOKEN", "")
    if not token:
        return None
    mode = normalize_mode(mode)
    arrival = None
    if arrive_by and mode == "drive":
        try:
            arrival = datetime.fromisoformat(arrive_by)
        except ValueError:
            arrival = None

    def directions(depart_at: datetime | None):
        params = {"access_token": token, "geometries": "geojson", "overview": "full", "steps": "true",
                  # No ferries: the Transit mode handles them, with their timetables.
                  "exclude": "ferry"}
        if depart_at and depart_at > datetime.now():
            params["depart_at"] = depart_at.strftime("%Y-%m-%dT%H:%M")   # local time at origin
        try:
            resp = requests.get(_DIRECTIONS_URL.format(PROFILES[mode], origin[0], origin[1],
                                                       destination[0], destination[1]),
                                params=params, timeout=20)
            resp.raise_for_status()
            best = (resp.json().get("routes") or [None])[0]
        except Exception:
            return None
        # exclude=ferry isn't airtight (Mapbox still routes walks onto the
        # Alameda Seaplane ferry), so a route with any ferry step is refused.
        if best and any(s.get("mode") == "ferry" for leg in best.get("legs") or []
                        for s in leg.get("steps") or []):
            return None
        return best

    if arrival:
        best = directions(arrival - timedelta(minutes=30))
        if best:
            best = directions(arrival - timedelta(seconds=best["duration"])) or best
    else:
        best = directions(None)
    if not best:
        return None
    typical = best.get("duration_typical")
    leave = arrival - timedelta(seconds=best["duration"]) if arrival else None
    return {
        # How this drive compares with usual traffic at that hour, in minutes
        # (positive = slower than usual). Driving only; None otherwise.
        "typical_min": round(typical / 60) if typical else None,
        "traffic_delta_min": round((best["duration"] - typical) / 60) if typical else None,
        "leave_by": leave.isoformat(timespec="minutes") if leave else None,
        "leave_by_text": leave.strftime("%I:%M %p").lstrip("0") if leave else None,
        "coords": [(c[0], c[1]) for c in best["geometry"]["coordinates"]],
        "eta_min": round(best["duration"] / 60),
        "distance_mi": round(best["distance"] / 1609.34, 1),
    }


# A big crowd reaches further than a crash: a sold-out arena backs up streets
# a kilometre away. Major events (15,000+) get wider thresholds.
EVENT_RED_KM = {True: 1.0, False: NEAR_RED_KM}
EVENT_YELLOW_KM = {True: 3.0, False: NEAR_YELLOW_KM}


# When a trip's alert goes out. A morning trip is announced the evening
# before, so there's time to change plans; a trip from noon on, that morning,
# when the forecast and the disruption pool are fresher. The clock
# (POST /api/tick) sends each one at the first tick after this time.
EVENING_BEFORE = time(20, 0)
MORNING_OF = time(7, 0)


def alert_due_at(start: datetime) -> datetime:
    if start.hour < 12:
        return datetime.combine(start.date() - timedelta(days=1), EVENING_BEFORE)
    return datetime.combine(start.date(), MORNING_OF)


def on_route_severity(kind: str | None, km: float) -> str | None:
    """Red means it is likely to affect the trip; yellow is a note.

    Distance decides, except for long-term construction: 511 lists work zones
    that run for months with no daily hours, so being on one says nothing
    about whether a crew is out when you pass. It stays a note; for a drive,
    the live traffic comparison is what says whether anything is slow.
    The page applies the same rule (`onRouteSeverity`)."""
    if km > NEAR_YELLOW_KM:
        return None
    if km <= NEAR_RED_KM and kind != "construction":
        return "red"
    return "yellow"


def events_on_route(trip: dict, drawn: dict, mode: str) -> list[dict]:
    """Big events near the route whose busy window overlaps the trip.

    For transit the whole route counts, rail included: a game doesn't block
    BART's tracks, but it fills its trains. The reason says which it is."""
    arrival = trip.get("next_at") or trip.get("arrive_at")
    try:
        end = datetime.fromisoformat(arrival)
    except (TypeError, ValueError):
        return []
    start = end - timedelta(minutes=drawn.get("eta_min") or 0)
    lines = [drawn["coords"]] if mode == "transit" else (drawn.get("street_lines") or [drawn["coords"]])
    found = []
    for e in events.during(start, end):
        km = min(point_to_line_km((e["lon"], e["lat"]), line) for line in lines)
        severity = ("red" if km <= EVENT_RED_KM[e["major"]]
                    else "yellow" if km <= EVENT_YELLOW_KM[e["major"]] else None)
        if not severity:
            continue
        effect = "expect crowded trains and buses" if mode == "transit" else "expect heavy traffic"
        found.append({"name": e["name"], "type": "event", "severity": severity,
                      "reason": f"{events.describe(e)}; {effect} ({km:.1f} km from route)"})
    return found


def build_context(trip: dict) -> dict | None:
    """Assemble the same trip context the browser POSTs to /api/brief."""
    # Your real home if you've set one (places.py), else the demo start.
    home = places.home()
    mode = normalize_mode(trip.get("mode"))
    drawn = route((home["lon"], home["lat"]), (trip["dest_lon"], trip["dest_lat"]), mode=mode,
                  arrive_by=trip.get("next_at") or trip.get("arrive_at"))
    if not drawn:
        return None

    on_route = []
    lines = drawn.get("street_lines") or [drawn["coords"]]
    for disruption in mock_data.get_disruptions():
        km = min(point_to_line_km((disruption["lon"], disruption["lat"]), line) for line in lines)
        severity = on_route_severity(disruption.get("type"), km)
        if not severity:
            continue
        on_route.append({
            "name": disruption["name"],
            "type": disruption.get("type"),
            "severity": severity,
            "reason": f"{disruption.get('note', '')} ({disruption.get('time', '')} · {km:.1f} km from route)",
        })
    on_route += events_on_route(trip, drawn, mode)

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
        "transit": transit_summary(drawn.get("transit")),
        "traffic_delta_min": drawn.get("traffic_delta_min"),
        "typical_min": drawn.get("typical_min"),
        "leave_by_text": drawn.get("leave_by_text"),
    }


def traffic_phrase(ctx: dict) -> str | None:
    """'7 min slower than usual', 'about usual', or None when unknown. Under
    three minutes either way is noise, not news."""
    delta = ctx.get("traffic_delta_min")
    if delta is None:
        return None
    if delta >= 3:
        return f"{delta} min slower than usual"
    if delta <= -3:
        return f"{-delta} min faster than usual"
    return "about usual for that time"


def transit_summary(plan: dict | None) -> dict | None:
    """The parts of a transit plan the brief and the email use, without geometry."""
    if not plan:
        return None
    return {
        "leave_text": plan.get("leave_text"),
        "arrive_text": plan.get("arrive_text"),
        "steps": [describe_segment(s) for s in plan.get("segments") or []],
        "live": live_summary(plan),
    }


def live_summary(plan: dict) -> list[str]:
    """What live data says about this trip, as sentences: cancellations and
    missed connections first, then real delays (5+ min) and service alerts.
    Empty when there's no live data or nothing worth saying."""
    live = plan.get("live") or {}
    lines = list(live.get("warnings") or [])
    for seg in plan.get("segments") or []:
        ride_live = seg.get("live") or {}
        name = seg.get("line") or seg.get("line_name") or seg.get("agency")
        delay = ride_live.get("delay_s")
        if ride_live.get("status") == "late" and delay and delay >= 5 * 60:
            lines.append(f"The {name} is running {round(delay / 60)} min late")
        for alert in ride_live.get("alerts") or []:
            if alert.get("header"):
                lines.append(f"{seg.get('agency')} alert: {alert['header']}")
    return lines


def describe_segment(seg: dict) -> str:
    """One itinerary line in plain words, e.g. "Take the N Judah light rail
    toward Ocean Beach from Embarcadero (8:12 AM), 6 stops, get off at
    Church St & Duboce Ave"."""
    if seg.get("kind") == "stop":
        when = f" ({seg['arrive_text']})" if seg.get("arrive_text") else ""
        return f"Arrive at {seg.get('name')}{when}"
    if seg.get("kind") == "transit":
        short, name = seg.get("line") or "", seg.get("line_name") or ""
        label = f"{short} {name}" if short and name and name != short else (short or name)
        vehicle = (seg.get("vehicle") or "transit").lower()
        line = label if vehicle in label.lower() else f"{label} {vehicle}".strip()
        toward = f" toward {seg['headsign']}" if seg.get("headsign") else ""
        count = seg.get("stops")
        stops = f", {count} stop{'' if count == 1 else 's'}" if count else ""
        when = f" ({seg['depart_text']})" if seg.get("depart_text") else ""
        return (f"Take the {line}{toward} from {seg.get('from_stop')}{when}"
                f"{stops}, get off at {seg.get('to_stop')}")
    minutes = max(1, round((seg.get("duration_s") or 0) / 60))
    return f"Walk {minutes} min to {seg.get('to') or 'your destination'}"


def push_payload(trip: dict, alert: dict, context: dict) -> dict:
    """The phone notification for one scheduled trip: short enough for a lock
    screen. Title says which trip and when to leave; body says why."""
    leave = context.get("leave_by_text") or (context.get("transit") or {}).get("leave_text")
    label = trip.get("label") or "Your trip"
    title = f"{label} · leave by {leave}" if leave else label
    risk = (alert.get("risk") or "none").lower()
    body = alert.get("headline") or "Your trip looks clear."
    if alert.get("recommendation") and alert["recommendation"] != "No action needed." and risk != "none":
        body = f"{body} {alert['recommendation']}"
    return {"title": title[:80], "body": body[:240], "url": "/",
            # One notification per trip occurrence: a resend replaces it.
            "tag": f"trip-{trip.get('id')}-{trip.get('next_at')}", "risk": risk}


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
    traffic = traffic_phrase(context)
    if traffic:
        lines.insert(5, f"Traffic:     {traffic}")
    if context.get("leave_by_text"):
        lines.insert(4, f"Leave by:    {context['leave_by_text']}")
    plan = context.get("transit")
    if plan:
        if plan.get("leave_text"):
            lines.insert(4, f"Leave by:    {plan['leave_text']}")
        lines += ["", "Your route:"] + [f"  {i}. {s}" for i, s in enumerate(plan["steps"], 1)]
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
