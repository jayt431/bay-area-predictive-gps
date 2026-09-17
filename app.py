"""
Flask API for the Predictive Route Intelligence Agent.

Endpoints:
  GET  /api/routes          - list available routes
  POST /api/analyze         - analyze a route for a date, return alert
  GET  /api/health          - health check

POST /api/analyze body: { "route_id": "rt_alex_home", "date": "YYYY-MM-DD" }

If ANTHROPIC_API_KEY is set, runs the full agent and parses its structured
alert. Without it, returns the raw tool data so the app is always demoable.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request

import mock_data
import tools
import agent
from agent import RouteIntelligenceAgent, api_key_present

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html", mapbox_token=os.environ.get("MAPBOX_TOKEN", ""))


@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "anthropic_key": api_key_present(),
        "weather_key": bool(os.environ.get("OPENWEATHER_API_KEY")),
        "news_key": bool(os.environ.get("NEWSAPI_KEY")),
    })


@app.route("/api/routes")
def get_routes():
    routes = [
        {
            "route_id": r["route_id"],
            "user": r["user"],
            "label": r["label"],
            "origin": r["origin"],
            "destination": r["destination"],
            "usual_departure": r["usual_departure"],
            "lat": r["lat"],
            "lon": r["lon"],
        }
        for r in mock_data.ROUTINES.values()
    ]
    return jsonify(routes)


@app.route("/api/pins")
def get_pins():
    """Map pins (red/yellow disruptions) for a route on a date."""
    route_id = request.args.get("route_id", "").strip()
    date = request.args.get("date", "").strip()
    if not route_id or not date:
        return jsonify({"error": "route_id and date query params are required"}), 400
    try:
        origin = mock_data.get_routine(route_id)
    except KeyError:
        return jsonify({"error": f"unknown route_id: {route_id}"}), 404
    return jsonify({
        "route_id": route_id,
        "date": date,
        "origin": {"lat": origin["lat"], "lon": origin["lon"]},
        "pins": mock_data.get_alert_pins(route_id, date),
    })


@app.route("/api/config")
def config():
    """Single-user config: the fixed home base the map starts from."""
    return jsonify({"home": mock_data.HOME})


@app.route("/api/disruptions")
def disruptions():
    """The Bay Area pool of possible disruptions. The frontend matches these
    against the drawn route and keeps only the ones that fall near it."""
    return jsonify({"disruptions": mock_data.get_disruptions()})


@app.route("/api/parking")
def parking():
    """Mocked parking zones around a destination (lat/lon query params)."""
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except ValueError:
        return jsonify({"error": "numeric lat and lon query params are required"}), 400
    return jsonify({"zones": mock_data.get_parking(lat, lon)})


@app.route("/api/meters")
def meters():
    """Real metered street parking near a point (SF DataSF), grouped by street."""
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except ValueError:
        return jsonify({"error": "numeric lat and lon query params are required"}), 400
    return jsonify(mock_data.get_metered_streets(lat, lon))


@app.route("/api/civic")
def civic():
    """Live civic/mobility events near a point (SF 311). One source, two views:
    the GPS on-route detection and the Area Feed both consume this."""
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except ValueError:
        return jsonify({"error": "numeric lat and lon query params are required"}), 400
    radius = request.args.get("radius", 1500, type=int)
    return jsonify(mock_data.get_civic_events(lat, lon, radius=radius))


# The map app is single-user out of a fixed home base, so news is scoped to the
# city that home sits in rather than to a per-route corridor.
_BRIEF_NEWS_AREA = "San Francisco"

try:  # stdlib on 3.9+, but needs system tzdata present
    from zoneinfo import ZoneInfo

    _PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover - fall back to a fixed offset
    _PACIFIC = None


def _local_now() -> datetime:
    """Now in Bay Area local time. The server clock is UTC on Render, and the
    trip window has to be expressed in the same local hours the forecast uses."""
    if _PACIFIC is not None:
        return datetime.now(_PACIFIC)
    return datetime.utcnow() - timedelta(hours=8)


def _weather_for_trip(lat: float, lon: float, now: datetime, eta_min: int,
                     area: str) -> dict:
    """Forecast covering the hours the trip actually runs in.

    Two constraints make the window non-obvious: the forecast only returns
    steps from now forward, so the window must look ahead rather than behind,
    and the steps are 3 hours apart, so a window as short as a real drive can
    fall between two of them and match nothing.
    """
    span = max(3, (eta_min // 60) + 1)
    forecast = mock_data.get_weather_at(
        lat, lon, now.strftime("%Y-%m-%d"), now.hour, min(now.hour + span, 23), area
    )
    if not forecast.get("error") and forecast.get("temp_f") is None and now.hour + span > 23:
        # Trip runs past midnight; the step covering it belongs to tomorrow.
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        forecast = mock_data.get_weather_at(
            lat, lon, tomorrow, 0, (now.hour + span) - 24, area
        )
    return forecast


def _live_conditions(ctx: dict) -> tuple[dict, dict]:
    """Live weather + news for the trip about to start, fetched in parallel.

    Never raises and never blocks the brief for long: each source already has
    its own request timeout and returns a dict carrying an `error` key when it
    cannot answer, which the prompt renders as "unavailable".
    """
    now = _local_now()
    date = now.strftime("%Y-%m-%d")
    try:
        eta_min = int(float(ctx.get("eta_min") or 0))
    except (TypeError, ValueError):
        eta_min = 0

    home = mock_data.HOME

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            w = pool.submit(_weather_for_trip, home["lat"], home["lon"], now,
                            eta_min, home["label"])
            n = pool.submit(mock_data.get_news_for_area, _BRIEF_NEWS_AREA, date)
            return w.result(timeout=15), n.result(timeout=15)
    except Exception as exc:
        note = f"live conditions unavailable: {exc}"
        return {"error": note, "alerts": []}, {"error": note, "articles": []}


@app.route("/api/brief", methods=["POST"])
def brief():
    """Pre-trip alert for the current route. Uses Claude when ANTHROPIC_API_KEY
    is set; otherwise returns a rule-based fallback so the UI always works.

    The frontend sends the concrete trip (destination, ETA, on-route
    disruptions, parking); the server adds live weather and news for the trip
    window before reasoning over the whole picture."""
    ctx = request.get_json(silent=True) or {}
    ctx["weather"], ctx["news"] = _live_conditions(ctx)
    if api_key_present():
        try:
            alert = _parse_alert(agent.trip_brief_text(ctx))
            return jsonify({"source": "ai", "alert": alert})
        except Exception as exc:
            # Never break the UI on an API hiccup — fall back.
            return jsonify({"source": "fallback", "note": str(exc), "alert": _fallback_brief(ctx)})
    return jsonify({"source": "fallback", "alert": _fallback_brief(ctx)})


def _fallback_brief(ctx: dict) -> dict:
    """Deterministic brief from the assembled trip, no model call."""
    disruptions = ctx.get("disruptions") or []
    reds = [d for d in disruptions if d.get("severity") == "red"]
    yellows = [d for d in disruptions if d.get("severity") == "yellow"]
    dest = ctx.get("destination", "your destination")

    if reds:
        risk = "high"
        headline = f"{reds[0]['name']} is likely to affect your trip to {dest}."
        why = f"{len(reds)} disruption(s) sit on your route, including {reds[0]['name']}. " + reds[0].get("reason", "")
        rec = "Consider leaving earlier or taking an alternate route."
    elif yellows:
        risk = "low"
        headline = f"Minor possible friction on the way to {dest}."
        why = f"{len(yellows)} item(s) are near your route but may not affect it, e.g. {yellows[0]['name']}."
        rec = "No action needed, but keep an eye out near the flagged area."
    else:
        risk = "none"
        headline = f"Clear run to {dest}."
        why = "No disruptions were detected on your route."
        rec = "No action needed."

    parking = ctx.get("parking") or []
    if parking:
        why += f" Metered parking is available near the destination (e.g. {parking[0].get('name')})."

    # Weather only moves the needle when the forecast raised a real alert (rain
    # likely or strong wind). News relevance is a judgment call, so the
    # rule-based path leaves headlines to the model rather than guessing.
    weather_alerts = (ctx.get("weather") or {}).get("alerts") or []
    if weather_alerts:
        why += " " + " ".join(weather_alerts)
        if risk == "none":
            risk = "low"
            headline = f"Clear route to {dest}, but check the weather."
        if rec == "No action needed.":
            rec = "Allow a little extra time for the conditions."
    return {"risk": risk, "headline": headline, "why": why, "recommendation": rec, "raw": None}


@app.route("/map-test")
def map_test():
    """Throwaway page to verify the Mapbox token and dark style render."""
    return render_template("map_test.html", mapbox_token=os.environ.get("MAPBOX_TOKEN", ""))


@app.route("/api/analyze", methods=["POST"])
def analyze():
    body = request.get_json(silent=True) or {}
    route_id = body.get("route_id", "").strip()
    date = body.get("date", "").strip()

    if not route_id or not date:
        return jsonify({"error": "route_id and date are required"}), 400

    try:
        routine = mock_data.get_routine(route_id)
    except KeyError:
        return jsonify({"error": f"unknown route_id: {route_id}"}), 404

    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400

    if api_key_present():
        return jsonify(_full_run(routine, date))
    else:
        return jsonify(_dry_run(route_id, date))


def _full_run(routine: dict, date: str) -> dict:
    agent = RouteIntelligenceAgent(verbose=False)
    alert_text = agent.analyze(routine, date)
    return {"mode": "agent", "route_id": routine["route_id"], "date": date, "alert": _parse_alert(alert_text)}


def _dry_run(route_id: str, date: str) -> dict:
    return {
        "mode": "dry_run",
        "note": "ANTHROPIC_API_KEY not set — returning raw tool data only.",
        "route_id": route_id,
        "date": date,
        "data": {
            "events": tools.run_tool("get_events_near_route", {"route_id": route_id, "date": date}),
            "weather": tools.run_tool("get_weather_forecast", {"route_id": route_id, "date": date}),
            "traffic_baseline": tools.run_tool("get_traffic_baseline", {"route_id": route_id}),
            "news": tools.run_tool("get_local_news", {"route_id": route_id, "date": date}),
        },
    }


def _parse_alert(text: str) -> dict:
    """Pull the structured fields out of the agent's final text block."""
    fields = {"risk": None, "headline": None, "why": None, "recommendation": None, "raw": text}
    patterns = {
        "risk": r"RISK:\s*(.+)",
        "headline": r"HEADLINE:\s*(.+)",
        "why": r"WHY:\s*([\s\S]+?)(?=RECOMMENDATION:|$)",
        "recommendation": r"RECOMMENDATION:\s*([\s\S]+?)$",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            fields[key] = match.group(1).strip()
    return fields


if __name__ == "__main__":
    app.run(debug=True, port=5000)
