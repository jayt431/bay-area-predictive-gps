"""
Tool definitions the agent exposes to Claude, plus a dispatcher.

Every tool now takes a `route_id` (and a date where relevant). The tool
resolves coordinates and corridor internally, so Claude never handles raw
latitude/longitude. Weather and news are live; events and traffic are mocked.
"""

from __future__ import annotations

import json

import mock_data

TOOLS = [
    {
        "name": "get_events_near_route",
        "description": (
            "Look up scheduled events (concerts, sports, festivals, community "
            "gatherings) for this route on a date, tagged with whether each sits "
            "on the route corridor and its distance from the route. Use this to "
            "judge whether an event could realistically add traffic to THIS "
            "commute. (Mocked data source in the current build.)"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "route_id": {"type": "string", "description": "The route identifier."},
                "date": {"type": "string", "description": "Date to check, YYYY-MM-DD."},
            },
            "required": ["route_id", "date"],
        },
    },
    {
        "name": "get_weather_forecast",
        "description": (
            "Get the LIVE weather forecast for the route's area, already filtered "
            "to the user's commute window on the given date, with any derived "
            "alerts. Use this to judge whether conditions overlap the travel time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "route_id": {"type": "string", "description": "The route identifier."},
                "date": {"type": "string", "description": "Date to check, YYYY-MM-DD."},
            },
            "required": ["route_id", "date"],
        },
    },
    {
        "name": "get_traffic_baseline",
        "description": (
            "Get the typical congestion this route normally sees at the user's "
            "usual departure time. Use this as the baseline to compare any event "
            "or weather disruption against, so you can tell an unusual day from a "
            "normal one. (Mocked data source in the current build.)"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "route_id": {"type": "string", "description": "The route identifier."},
            },
            "required": ["route_id"],
        },
    },
    {
        "name": "get_local_news",
        "description": (
            "Get recent LIVE Bay Area news relevant to this route, such as road "
            "closures, construction, protests, crashes, or flooding. News is "
            "unstructured, so read each headline and decide whether it plausibly "
            "affects THIS corridor. Use it to catch disruptions events and "
            "weather feeds miss."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "route_id": {"type": "string", "description": "The route identifier."},
                "date": {"type": "string", "description": "Date being analyzed, YYYY-MM-DD."},
            },
            "required": ["route_id", "date"],
        },
    },
]


_HANDLERS = {
    "get_events_near_route": lambda a: mock_data.get_events_near_route(a["route_id"], a["date"]),
    "get_weather_forecast": lambda a: mock_data.get_weather_forecast(a["route_id"], a["date"]),
    "get_traffic_baseline": lambda a: mock_data.get_traffic_baseline(a["route_id"]),
    "get_local_news": lambda a: mock_data.get_local_news(a["route_id"], a["date"]),
}


def run_tool(name: str, arguments: dict) -> str:
    handler = _HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"unknown tool: {name}"})
    try:
        return json.dumps(handler(arguments), indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
