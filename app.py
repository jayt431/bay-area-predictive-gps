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

from flask import Flask, jsonify, render_template, request

import mock_data
import tools
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
